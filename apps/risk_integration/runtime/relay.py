"""The only sender. It claims rows with a lease, sends, then commits the send. A crash or a lost session between
the send and the commit sends again, which the provider deduplicates by `ticket_id`.

Every session starts by re-sending what the previous session may have lost: `TICKET` for every `SENT` ticket
(the provider does not replay recommendations), and every final message sent within the loss window before the
previous session ended. Only then does the relay poll the outbox.

A draining process stops the relay and calls `flush` once: the final messages owed go out, a `TICKET` does not.
"""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import NoReturn, Protocol

import structlog
from structlog.contextvars import bound_contextvars

from apps.risk_integration.domain.states import CLOSED_FINAL, State
from apps.risk_integration.persistence import tickets
from apps.risk_integration.persistence.db import DbRunner
from apps.risk_integration.persistence.errors import TRANSIENT_DB_ERRORS, transient_kind
from apps.risk_integration.protocol.codec import encode
from apps.risk_integration.protocol.messages import (
    CancellationReason,
    ConfirmRecommendation,
    DeclineReason,
    DeclineRecommendation,
    OutboundMessage,
    RequestCancellation,
    Ticket,
)
from apps.risk_integration.runtime import metrics
from apps.risk_integration.runtime.health import Heartbeat

log = structlog.get_logger(__name__)

type Claim = Callable[[], list[tickets.ClaimedTicket]]

# A re-send carries the same message as the first send; only the metric tells them apart.
_RESENT = frozenset({State.SENT}) | CLOSED_FINAL


class Sender(Protocol):
    async def send(self, frame: str) -> None:
        """Raises `SessionEnded` when the session is gone."""
        ...


@dataclass(frozen=True)
class RelaySettings:
    owner: str
    lease: timedelta
    batch_size: int
    poll_interval: timedelta
    retransmit_after: timedelta
    loss_window: timedelta


class Relay:
    def __init__(self, run_db: DbRunner, settings: RelaySettings, heartbeat: Heartbeat) -> None:
        self._run_db = run_db
        self._settings = settings
        self._heartbeat = heartbeat

    async def run(self, session: Sender) -> NoReturn:
        resend: deque[Claim] | None = None
        batch: list[tickets.ClaimedTicket] = []
        try:
            while True:
                self._heartbeat.beat()
                try:
                    if resend is None:
                        resend = await self._resend_owed()
                    batch = await self._next_batch(resend)
                except TRANSIENT_DB_ERRORS as exc:
                    _count_db_error(exc)
                    await asyncio.sleep(self._settings.poll_interval.total_seconds())
                    continue
                for claimed in batch:
                    await self._send(session, claimed)
                    self._heartbeat.beat()
                if not resend and len(batch) < self._settings.batch_size:
                    await asyncio.sleep(self._settings.poll_interval.total_seconds())
        finally:
            # Runs on a failed send and on cancellation alike, before the next session starts.
            self._heartbeat.park()
            await self._release(batch)

    async def flush(self, session: Sender) -> int:
        """One pass over the final messages owed, for a draining process: the provider learns every decision
        this process committed. Returns how many were sent. Raises like a send or a claim does."""
        settings = self._settings
        claim = partial(
            tickets.claim_final_pending, owner=settings.owner, lease=settings.lease, limit=settings.batch_size
        )
        batch: list[tickets.ClaimedTicket] = []
        sent = 0
        try:
            while True:
                batch = await self._claim(claim)
                for claimed in batch:
                    await self._send(session, claimed)
                    sent += 1
                if len(batch) < settings.batch_size:
                    return sent
        finally:
            await self._release(batch)

    async def _resend_owed(self) -> deque[Claim]:
        """The re-send queries for this session. Each pages by `since`: a re-send moves `last_sent_at` past it."""
        since = await self._run_db(tickets.db_now)
        # When the previous session died is not known, after a restart least of all. Its last final send is: no
        # final goes out between two sessions, so a window ending there covers all a dead connection can lose.
        last_final = await self._run_db(tickets.last_final_sent_at)
        settings = self._settings
        owed: deque[Claim] = deque(
            [
                partial(
                    tickets.claim_sent_for_resend,
                    owner=settings.owner,
                    lease=settings.lease,
                    limit=settings.batch_size,
                    since=since,
                )
            ]
        )
        if last_final is not None:
            owed.append(
                partial(
                    tickets.claim_finals_for_resend,
                    owner=settings.owner,
                    lease=settings.lease,
                    limit=settings.batch_size,
                    window_start=last_final - settings.loss_window,
                    since=since,
                )
            )
        log.info("resend_started", since=since.isoformat(), window_end=last_final.isoformat() if last_final else None)
        return owed

    async def _next_batch(self, resend: deque[Claim]) -> list[tickets.ClaimedTicket]:
        while resend:
            batch = await self._claim(resend[0])
            if batch:
                return batch
            resend.popleft()
        settings = self._settings
        return await self._claim(
            partial(tickets.claim_due, owner=settings.owner, lease=settings.lease, limit=settings.batch_size)
        )

    async def _claim(self, claim: Claim) -> list[tickets.ClaimedTicket]:
        # Cancelling the caller does not stop the unit of work. A claim that commits after the cancellation would
        # hold its rows for a whole lease with nobody to send them, so it is waited out (bounded by the run_db
        # timeout) and what it took is released.
        claiming = asyncio.ensure_future(self._run_db(claim))
        try:
            return await asyncio.shield(claiming)
        except asyncio.CancelledError:
            try:
                await self._release(await claiming)
            except TRANSIENT_DB_ERRORS as exc:
                _count_db_error(exc)
            raise

    async def _send(self, session: Sender, claimed: tickets.ClaimedTicket) -> None:
        message = _message(claimed)
        with bound_contextvars(ticket_id=str(claimed.ticket_id), bet_id=claimed.bet_id):
            await session.send(encode(message, ts=datetime.now(UTC)))
            attempt = "resend" if claimed.state in _RESENT else "first"
            metrics.MESSAGES_SENT.labels(type=message.message_type, attempt=attempt).inc()
            try:
                recorded = await self._run_db(
                    tickets.record_send,
                    claimed.ticket_id,
                    claimed.state,
                    owner=self._settings.owner,
                    retransmit_after=self._settings.retransmit_after,
                )
            except TRANSIENT_DB_ERRORS as exc:
                # The lease runs out and the message goes again; the provider deduplicates.
                _count_db_error(exc)
                return
            if not recorded:
                log.debug("send_not_recorded", state=claimed.state)

    async def _release(self, batch: list[tickets.ClaimedTicket]) -> None:
        """Frees the rest of a batch the session did not get to send, so the next session sends it at once.
        Best effort: if this fails, the leases run out."""
        if not batch:
            return
        try:
            released = await self._run_db(
                tickets.release_leases, [c.ticket_id for c in batch], owner=self._settings.owner
            )
        except TRANSIENT_DB_ERRORS as exc:
            _count_db_error(exc)
            return
        if released:
            log.info("leases_released", count=released)


def _message(claimed: tickets.ClaimedTicket) -> OutboundMessage:
    """The message owed in the claimed state; a re-send is the same message as the first send."""
    ticket_id = claimed.ticket_id
    match claimed.state:
        case State.PENDING_SEND | State.SENT:
            s = claimed.snapshot
            return Ticket(
                ticket_id=ticket_id,
                stake=s.stake,
                currency=s.currency,
                odds=s.odds,
                selection_id=s.selection_id,
                customer_ref=s.customer_ref,
            )
        case State.CONFIRM_PENDING | State.CONFIRMED:
            return ConfirmRecommendation(ticket_id=ticket_id)
        case State.DECLINE_PENDING | State.DECLINED:
            reason = DeclineReason(claimed.decline_reason) if claimed.decline_reason else None
            return DeclineRecommendation(ticket_id=ticket_id, reason=reason)
        case State.CANCEL_PENDING | State.CANCELLED if claimed.cancel_reason is not None:
            return RequestCancellation(ticket_id=ticket_id, reason=CancellationReason(claimed.cancel_reason.value))
    raise ValueError(f"nothing to send in state {claimed.state}")


def _count_db_error(exc: Exception) -> None:
    kind = transient_kind(exc)
    log.warning("relay_db_error", kind=kind, error=type(exc).__name__)
    metrics.DB_ERRORS.labels(loop="relay", kind=kind).inc()
