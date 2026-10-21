"""The provider session and the loops bound to it: the reader, the relay, and a watch on the connection.

The connection task holds a session for as long as the process runs. When one ends or is refused, it waits by
how it ended and connects again; only a bug ends the task, and with it the process. Every session starts with
the relay's re-send, so whatever the previous session may have lost goes out again.
"""

import asyncio
import time
from datetime import timedelta
from typing import NoReturn

import structlog
from structlog.contextvars import bound_contextvars

from apps.risk_integration.protocol.close_codes import SessionOutcome
from apps.risk_integration.runtime import metrics
from apps.risk_integration.runtime.backoff import Backoff
from apps.risk_integration.runtime.inbound import InboundQueue, read_frames
from apps.risk_integration.runtime.relay import Relay
from apps.risk_integration.runtime.supervisor import supervise
from apps.risk_integration.transport.ws_client import (
    ConnectOptions,
    ProviderSession,
    SessionEnded,
    TokenProvider,
    fetch_token,
    open_session,
)

log = structlog.get_logger(__name__)


async def run_connection(
    options: ConnectOptions, tokens: TokenProvider, relay: Relay, queue: InboundQueue, backoff: Backoff
) -> NoReturn:
    while True:
        outcome, lasted = await _hold_session(options, tokens, relay, queue)
        delay = backoff.next_delay(outcome, lasted=lasted)
        log.info("reconnect_scheduled", outcome=outcome, delay_seconds=round(delay.total_seconds(), 3))
        await asyncio.sleep(delay.total_seconds())


async def _hold_session(
    options: ConnectOptions, tokens: TokenProvider, relay: Relay, queue: InboundQueue
) -> tuple[SessionOutcome, timedelta | None]:
    """Connects and runs one session until it ends. Returns how it ended and how long it was open, `None` when it
    was refused before it opened."""
    opened: float | None = None
    connection_id: str | None = None
    try:
        token = await fetch_token(tokens, within=options.token_timeout)
        async with open_session(options, token) as session:
            opened, connection_id = time.monotonic(), session.connection_id
            metrics.SESSION_OPEN.set(1)
            with bound_contextvars(connection_id=connection_id):
                log.info("session_opened")
                await _run_session(session, relay, queue)
    except SessionEnded as ended:
        lasted = None if opened is None else timedelta(seconds=time.monotonic() - opened)
        _record_end(ended, lasted, connection_id=connection_id, queue_full=queue.full())
        return ended.outcome, lasted
    finally:
        metrics.SESSION_OPEN.set(0)


async def _run_session(session: ProviderSession, relay: Relay, queue: InboundQueue) -> NoReturn:
    try:
        await supervise(
            {
                "reader": lambda: read_frames(session, queue),
                "relay": lambda: relay.run(session),
                "watch": session.closed,
            }
        )
    except* SessionEnded as ended:
        # Raised alone, it replaces the group; next to a bug in the reader or the relay, both stay in a group
        # and the bug ends the process.
        raise ended.exceptions[0] from None


def _record_end(ended: SessionEnded, lasted: timedelta | None, *, connection_id: str | None, queue_full: bool) -> None:
    # A full queue when the session dropped means the reader had stopped reading, pongs included: the keepalive
    # dropped it because the appliers fell behind, not because the provider went away.
    level = log.error if ended.outcome is SessionOutcome.AUTH_FAILED else log.warning
    level(
        "session_ended",
        outcome=ended.outcome,
        detail=str(ended),
        connection_id=connection_id,
        lasted_seconds=None if lasted is None else round(lasted.total_seconds(), 3),
        inbound_queue_full=queue_full,
    )
    metrics.SESSIONS_ENDED.labels(outcome=ended.outcome, inbound_queue_full=str(queue_full).lower()).inc()
