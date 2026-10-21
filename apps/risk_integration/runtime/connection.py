"""The provider session and the loops bound to it: the reader, the relay, and a watch on the connection.

One session per process for now: when it ends, the process exits and Kubernetes restarts it, and the next
process re-sends what was in flight. Nothing is lost that way, only time.
"""

import structlog
from structlog.contextvars import bound_contextvars

from apps.risk_integration.runtime import metrics
from apps.risk_integration.runtime.inbound import InboundQueue, read_frames
from apps.risk_integration.runtime.relay import Relay
from apps.risk_integration.runtime.supervisor import supervise
from apps.risk_integration.transport.ws_client import (
    ConnectOptions,
    ProviderSession,
    SessionEnded,
    TokenProvider,
    open_session,
)

log = structlog.get_logger(__name__)


async def run_connection(options: ConnectOptions, tokens: TokenProvider, relay: Relay, queue: InboundQueue) -> None:
    """Raises `SessionEnded` when the session is refused or ends; never returns."""
    try:
        async with open_session(options, await tokens.token()) as session:
            with bound_contextvars(connection_id=session.connection_id):
                log.info("session_opened")
                await _run_session(session, relay, queue)
    except SessionEnded as ended:
        log.warning("session_ended", outcome=ended.outcome, detail=str(ended))
        metrics.SESSIONS_ENDED.labels(outcome=ended.outcome).inc()
        raise


async def _run_session(session: ProviderSession, relay: Relay, queue: InboundQueue) -> None:
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
