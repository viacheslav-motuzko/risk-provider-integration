"""The provider session and the loops bound to it: the reader, the relay, and a watch on the connection.

The connection task holds a session for as long as the process runs. When one ends or is refused, it waits by
how it ended and connects again. Every session starts with the relay's re-send, so whatever the previous session
may have lost goes out again. On a shutdown request it drains the open session and raises `Drained`; anything
else that ends it is a bug, and ends the process.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import NoReturn

import structlog
from structlog.contextvars import bound_contextvars

from apps.risk_integration.persistence.errors import TRANSIENT_DB_ERRORS
from apps.risk_integration.protocol.close_codes import CLOSE_GOING_AWAY, SessionOutcome
from apps.risk_integration.runtime import metrics
from apps.risk_integration.runtime.backoff import Backoff
from apps.risk_integration.runtime.health import Heartbeat
from apps.risk_integration.runtime.inbound import InboundQueue, read_frames
from apps.risk_integration.runtime.relay import Relay
from apps.risk_integration.runtime.shutdown import Drained, Shutdown, StopRequested
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


@dataclass(frozen=True)
class Connection:
    options: ConnectOptions
    tokens: TokenProvider
    relay: Relay
    queue: InboundQueue
    backoff: Backoff
    shutdown: Shutdown
    drain_timeout: timedelta
    heartbeat: Heartbeat


async def run_connection(c: Connection) -> NoReturn:
    while not c.shutdown.requested:
        c.heartbeat.beat(extra=c.options.token_timeout + c.options.open_timeout)
        outcome, lasted = await _hold_session(c)
        delay = c.backoff.next_delay(outcome, lasted=lasted)
        log.info("reconnect_scheduled", outcome=outcome, delay_seconds=round(delay.total_seconds(), 3))
        c.heartbeat.beat(extra=delay)
        await c.shutdown.sleep(delay.total_seconds())
    raise Drained("no session to drain")


async def _hold_session(c: Connection) -> tuple[SessionOutcome, timedelta | None]:
    """Connects and runs one session until it ends. Returns how it ended and how long it was open, `None` when it
    was refused before it opened. Raises `Drained` when shutdown was requested and the session is drained."""
    opened: float | None = None
    connection_id: str | None = None
    try:
        token = await fetch_token(c.tokens, within=c.options.token_timeout)
        async with open_session(c.options, token) as session:
            opened, connection_id = time.monotonic(), session.connection_id
            metrics.SESSION_OPEN.set(1)
            with bound_contextvars(connection_id=connection_id):
                log.info("session_opened")
                # An open session can last for hours; its relay beats for it.
                c.heartbeat.park()
                try:
                    await _run_session(session, c.relay, c.queue, c.shutdown)
                except StopRequested:
                    await _drain(session, c.relay, c.queue, c.drain_timeout)
                    raise Drained("session drained") from None
    except SessionEnded as ended:
        lasted = None if opened is None else timedelta(seconds=time.monotonic() - opened)
        _record_end(ended, lasted, connection_id=connection_id, queue_full=c.queue.full())
        return ended.outcome, lasted
    finally:
        metrics.SESSION_OPEN.set(0)


async def _run_session(session: ProviderSession, relay: Relay, queue: InboundQueue, shutdown: Shutdown) -> NoReturn:
    try:
        await supervise(
            {
                "reader": lambda: read_frames(session, queue),
                "relay": lambda: relay.run(session),
                "watch": session.closed,
                "stop": shutdown.stop_session,
            }
        )
    except* (SessionEnded, StopRequested) as group:
        # Raised alone, it replaces the group; next to a bug in the reader or the relay, both stay in a group
        # and the bug ends the process. A session that ended as the stop came has nothing left to drain.
        ended = [e for e in group.exceptions if isinstance(e, SessionEnded)]
        raise ended[0] if ended else group.exceptions[0] from None


async def _drain(session: ProviderSession, relay: Relay, queue: InboundQueue, budget: timedelta) -> None:
    """The reader and the relay have stopped, so nothing new is read or claimed. The appliers finish what was
    read, the final messages they committed go out once, and the session closes with 1001. Bounded by `budget`;
    whatever is left over goes out on the next session, in this pod's successor or another instance."""
    log.info("drain_started", queued=queue.qsize())
    try:
        async with asyncio.timeout(budget.total_seconds()):
            await queue.join()
            flushed = await relay.flush(session)
    except TimeoutError:
        log.warning("drain_timed_out", queued=queue.qsize())
    except SessionEnded as ended:
        log.warning("drain_session_lost", detail=str(ended))
    except TRANSIENT_DB_ERRORS as exc:
        log.warning("drain_db_error", error=type(exc).__name__)
    else:
        log.info("drain_finished", flushed=flushed)
    await session.close(CLOSE_GOING_AWAY, "shutting down")


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
