"""Ticket persistence. Every function runs inside the caller's transaction: one `run_db` unit of work, or the
monolith's `atomic` for `create_ticket`. Claims hold row locks until that transaction commits, so a claim is a
unit of work of its own.

Transitions are conditional updates built from `domain.machine.RULES`; each clears the lease and returns whether
it applied. A miss (duplicate, late reply, lost lease) is a normal outcome, not an error.
Monolith side effects run in the same transaction, under the lock taken by `lock_awaiting` (`services.py`).
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import connection, transaction
from django.db.models import BooleanField, DateTimeField, DurationField, ExpressionWrapper, F, Max, Q, Value
from django.db.models.expressions import CombinedExpression
from django.db.models.functions import Now

from apps.risk_integration.domain.machine import (
    CANCEL_REASONS,
    ERROR_REFS,
    ESCALATION_REASONS,
    RULES,
    Trigger,
    send_trigger,
)
from apps.risk_integration.domain.states import (
    AWAITING_RECOMMENDATION,
    CLOSED_FINAL,
    FINAL_PENDING,
    CancelReason,
    Decision,
    DeclineReason,
    State,
)
from apps.risk_integration.models import RiskTicket

# A re-send lands in the next session's loss window: without a cap it would go again after every reconnect.
FINAL_RESENDS_MAX = 1


@dataclass(frozen=True)
class TicketSnapshot:
    stake: Decimal
    currency: str
    odds: Decimal
    selection_id: str
    customer_ref: str


@dataclass(frozen=True)
class ClaimedTicket:
    ticket_id: UUID
    bet_id: int
    state: State
    snapshot: TicketSnapshot
    decline_reason: str
    cancel_reason: CancelReason | None


@dataclass(frozen=True)
class AwaitingTicket:
    bet_id: int
    age: timedelta
    overdue: bool


def create_ticket(*, bet_id: int, snapshot: TicketSnapshot, deadline: timedelta) -> UUID:
    """Must run in the transaction that creates the bet, so neither exists without the other"""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("create_ticket must run inside the transaction that creates the bet")
    ticket = RiskTicket.objects.create(
        bet_id=bet_id,
        stake=snapshot.stake,
        currency=snapshot.currency,
        odds=snapshot.odds,
        selection_id=snapshot.selection_id,
        customer_ref=snapshot.customer_ref,
        deadline_at=_db_now_plus(deadline),  # the customer waits from creation, so the budget counts from it
    )
    return ticket.ticket_id


def claim_due(*, owner: str, lease: timedelta, limit: int) -> list[ClaimedTicket]:
    """The relay's normal pass: every message owed (OUTBOX) and SENT tickets at their retransmit point"""
    owed = Q(state__in=FINAL_PENDING) | _before_deadline(State.PENDING_SEND)
    retransmit = _before_deadline(State.SENT) & Q(retransmit_at__lte=Now())
    return _claim(owed | retransmit, owner=owner, lease=lease, limit=limit)


def claim_final_pending(*, owner: str, lease: timedelta, limit: int) -> list[ClaimedTicket]:
    """A draining relay's flush: final messages owed, never a `TICKET`."""
    return _claim(Q(state__in=FINAL_PENDING), owner=owner, lease=lease, limit=limit)


def claim_sent_for_resend(*, owner: str, lease: timedelta, limit: int, since: datetime) -> list[ClaimedTicket]:
    """After a (re)connect at DB time `since`: SENT tickets not yet re-sent on this session.
    A re-send moves `last_sent_at` past `since`, so calling this until it returns nothing pages through all."""
    return _claim(_before_deadline(State.SENT) & Q(last_sent_at__lt=since), owner=owner, lease=lease, limit=limit)


def claim_finals_for_resend(
    *, owner: str, lease: timedelta, limit: int, window_start: datetime, since: datetime
) -> list[ClaimedTicket]:
    """After a (re)connect at DB time `since`: final messages sent in `[window_start, since)`, whatever their
    type, and not yet re-sent `FINAL_RESENDS_MAX` times. Pages like `claim_sent_for_resend`."""
    due = Q(
        state__in=CLOSED_FINAL,
        final_resends__lt=FINAL_RESENDS_MAX,
        last_sent_at__gte=window_start,
        last_sent_at__lt=since,
    )
    return _claim(due, owner=owner, lease=lease, limit=limit)


def release_leases(ticket_ids: Collection[UUID], *, owner: str) -> int:
    """Clears `owner`'s leases on these tickets, for a relay whose session ended mid-batch: the next session can
    claim them at once instead of waiting the lease out. Rows already recorded have no lease and are untouched."""
    return RiskTicket.objects.filter(ticket_id__in=ticket_ids, locked_by=owner).update(locked_by="", locked_until=None)


def lock_awaiting(ticket_id: UUID) -> AwaitingTicket | None:
    """Locks a ticket that still waits for its recommendation and returns its bet, age and whether its deadline
    has passed, or None if it has moved on (duplicate, late). The lock serializes appliers and the sweeper on this
    ticket until commit, so the monolith decides once."""
    due = Q(ticket_id=ticket_id, state__in=AWAITING_RECOMMENDATION)
    age = ExpressionWrapper(Now() - F("created_at"), output_field=DurationField())
    overdue = ExpressionWrapper(Q(deadline_at__lte=Now()), output_field=BooleanField())
    row = RiskTicket.objects.select_for_update().filter(due).values_list("bet_id", age, overdue).first()
    return None if row is None else AwaitingTicket(bet_id=row[0], age=row[1], overdue=row[2])


def record_send(ticket_id: UUID, sent_from: State, *, owner: str, retransmit_after: timedelta) -> bool:
    """The relay sent the message owed in `sent_from` and still holds
    the lease. A crash before this commit makes the relay send again; the provider
    deduplicates by `ticket_id`."""
    trigger = send_trigger(sent_from)
    values: dict[str, Any] = {"last_sent_at": Now()}
    if trigger is Trigger.TICKET_SENT:
        values["retransmit_at"] = _db_now_plus(retransmit_after)
    elif trigger is Trigger.TICKET_RESENT:
        values["retransmit_at"] = None
    elif trigger is Trigger.FINAL_RESENT:
        values["final_resends"] = F("final_resends") + 1
    return _transition(ticket_id, trigger, owner=owner, **values)


def record_accept(ticket_id: UUID) -> bool:
    """The caller captures the stake in the same transaction when this returns True."""
    return _transition(ticket_id, Trigger.ACCEPT_APPLIED, recommendation=Decision.ACCEPT)


def record_decline(ticket_id: UUID) -> bool:
    """The caller rejects the bet and releases the stake in the same transaction when this returns True."""
    return _transition(ticket_id, Trigger.DECLINE_APPLIED, recommendation=Decision.DECLINE)


def record_domain_rejection(ticket_id: UUID, *, decline_reason: DeclineReason) -> bool:
    """ACCEPT, but our own rules reject the bet; we decline with `decline_reason`."""
    return _transition(
        ticket_id, Trigger.DOMAIN_REJECTED, recommendation=Decision.ACCEPT, decline_reason=decline_reason
    )


def cancel(ticket_id: UUID, trigger: Trigger, *, error_code: str = "") -> bool:
    """Our placement failed. The caller compensates in the same transaction when this returns True."""
    values: dict[str, Any] = {"cancel_reason": CANCEL_REASONS[trigger]}
    if trigger in ERROR_REFS:
        values.update(last_error_code=error_code, last_error_ref_type=ERROR_REFS[trigger])
    extra = Q(deadline_at__lte=Now()) if trigger is Trigger.DEADLINE_PASSED else None
    return _transition(ticket_id, trigger, extra=extra, **values)


def escalate(ticket_id: UUID, trigger: Trigger, *, error_code: str) -> bool:
    """The provider refused our confirm or cancellation. No money moves: the bet stays as it is and risk
    operations resolve the mismatch."""
    return _transition(
        ticket_id,
        trigger,
        escalation_reason=ESCALATION_REASONS[trigger],
        last_error_code=error_code,
        last_error_ref_type=ERROR_REFS[trigger],
    )


def find_expired(*, limit: int, exclude: Collection[UUID] = ()) -> list[UUID]:
    """Tickets still waiting for a recommendation at their deadline, for the sweeper, oldest first."""
    due = Q(state__in=AWAITING_RECOMMENDATION, deadline_at__lte=Now())
    if exclude:
        due &= ~Q(ticket_id__in=exclude)
    return list(RiskTicket.objects.filter(due).order_by("deadline_at").values_list("ticket_id", flat=True)[:limit])


def last_final_sent_at() -> datetime | None:
    """The latest send of a final message."""
    latest: datetime | None = RiskTicket.objects.filter(state__in=CLOSED_FINAL).aggregate(latest=Max("last_sent_at"))[
        "latest"
    ]
    return latest


def db_now() -> datetime:
    """DB time, for anchors the runtime keeps between units of work (the connect time of a session)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT statement_timestamp()")  # the clock of Django's Now() on PostgreSQL
        (value,) = cursor.fetchone()
    now: datetime = value
    return now


def _claim(due: Q, *, owner: str, lease: timedelta, limit: int) -> list[ClaimedTicket]:
    # No FIFO guarantee with SKIP LOCKED; the order is best-effort and nothing depends on it.
    rows = list(
        RiskTicket.objects.select_for_update(skip_locked=True).filter(due, _lease_free()).order_by("created_at")[:limit]
    )
    if rows:
        RiskTicket.objects.filter(ticket_id__in=[r.ticket_id for r in rows]).update(
            locked_by=owner, locked_until=_db_now_plus(lease)
        )
    return [
        ClaimedTicket(
            ticket_id=r.ticket_id,
            bet_id=r.bet_id,
            state=State(r.state),
            snapshot=TicketSnapshot(
                stake=r.stake,
                currency=r.currency,
                odds=r.odds,
                selection_id=r.selection_id,
                customer_ref=r.customer_ref,
            ),
            decline_reason=r.decline_reason,
            cancel_reason=CancelReason(r.cancel_reason) if r.cancel_reason else None,
        )
        for r in rows
    ]


def _lease_free() -> Q:
    return Q(locked_until__isnull=True) | Q(locked_until__lt=Now())


def _before_deadline(state: State) -> Q:
    return Q(state=state, deadline_at__gt=Now())


def _transition(
    ticket_id: UUID, trigger: Trigger, *, owner: str | None = None, extra: Q | None = None, **values: Any
) -> bool:
    rule = RULES[trigger]
    rows = RiskTicket.objects.filter(Q(ticket_id=ticket_id, state__in=rule.sources), extra or Q())
    if rule.by_lease_owner:
        if not owner:
            raise ValueError(f"{trigger} needs the lease owner")
        rows = rows.filter(locked_by=owner)
    if rule.target is not None:
        values["state"] = rule.target
    return rows.update(locked_by="", locked_until=None, **values) == 1


def _db_now_plus(delta: timedelta) -> "ExpressionWrapper[CombinedExpression]":
    return ExpressionWrapper(Now() + Value(delta, output_field=DurationField()), output_field=DateTimeField())
