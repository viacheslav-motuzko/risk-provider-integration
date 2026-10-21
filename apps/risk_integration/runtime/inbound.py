"""Inbound path: the reader decodes and enqueues, the appliers do the database work.

The queue is bounded. When the appliers fall behind (a slow database), the reader waits, the socket is no
longer read and the keepalive eventually drops the session; the retransmission on the next session brings the
in-flight recommendations back. Messages for one ticket may be applied concurrently: the ticket lock in each
unit of work serializes them.
"""

import asyncio
from typing import NoReturn

import structlog

from apps.risk_integration.domain.machine import Trigger, trigger_for_error
from apps.risk_integration.domain.states import Decision, MessageRef
from apps.risk_integration.protocol.codec import FrameError, decode
from apps.risk_integration.protocol.messages import InboundMessage, ProviderError, Recommendation
from apps.risk_integration.runtime import metrics
from apps.risk_integration.runtime.decisions import Decisions
from apps.risk_integration.transport.ws_client import ProviderSession

log = structlog.get_logger(__name__)

type InboundQueue = asyncio.Queue[InboundMessage]


async def read_frames(session: ProviderSession, queue: InboundQueue) -> NoReturn:
    while True:
        frame = await session.recv()
        try:
            message = decode(frame)
        except FrameError as exc:
            log.warning(
                "frame_dropped",
                kind=exc.kind,
                detail=exc.detail,
                message_type=exc.message_type,
                ticket_id=str(exc.ticket_id) if exc.ticket_id else None,
            )
            metrics.FRAMES_DROPPED.labels(kind=exc.kind).inc()
            continue
        await queue.put(message)


async def apply_messages(queue: InboundQueue, decisions: Decisions) -> NoReturn:
    while True:
        match await queue.get():
            case Recommendation() as recommendation:
                await decisions.apply(recommendation.ticket_id, Decision(recommendation.decision.value))
            case ProviderError() as error:
                await _on_provider_error(error, decisions)


async def _on_provider_error(error: ProviderError, decisions: Decisions) -> None:
    if error.ticket_id is None or error.ref_type is None:
        log.warning("provider_error_ignored", reason="uncorrelated", code=error.code)
        metrics.INBOUND_IGNORED.labels(message="ERROR", reason="uncorrelated").inc()
        return
    ref = MessageRef(error.ref_type.value)
    trigger = trigger_for_error(ref, unknown_ticket=error.is_unknown_ticket)
    if trigger is None:
        log.info("provider_error_ignored", reason="no_effect", ticket_id=str(error.ticket_id), ref=ref, code=error.code)
        metrics.INBOUND_IGNORED.labels(message="ERROR", reason="no_effect").inc()
    elif trigger is Trigger.TICKET_REFUSED:
        await decisions.compensate(error.ticket_id, trigger, error_code=error.code)
    else:
        await decisions.escalate(error.ticket_id, trigger, error_code=error.code)
