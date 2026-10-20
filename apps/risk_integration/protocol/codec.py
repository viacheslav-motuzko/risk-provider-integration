import json
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import ValidationError

from apps.risk_integration.protocol.envelope import Envelope
from apps.risk_integration.protocol.messages import (
    InboundMessage,
    InboundType,
    OutboundMessage,
    ProviderError,
    Recommendation,
)

_INBOUND: dict[str, type[Recommendation] | type[ProviderError]] = {
    InboundType.RECOMMENDATION: Recommendation,
    InboundType.ERROR: ProviderError,
}


class FrameErrorKind(StrEnum):
    """Bounded set of reasons, used as a metric label."""

    BINARY = "binary"
    NOT_JSON = "not_json"
    BAD_ENVELOPE = "bad_envelope"
    UNKNOWN_TYPE = "unknown_type"
    BAD_PAYLOAD = "bad_payload"


class FrameError(Exception):
    """An inbound frame that cannot be turned into a message. Permanent: the frame is counted and dropped.

    `message_type` and `ticket_id` are filled in when the envelope could be read, for the log line.
    """

    def __init__(
        self,
        kind: FrameErrorKind,
        detail: str,
        *,
        message_type: str | None = None,
        ticket_id: UUID | None = None,
    ) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.message_type = message_type
        self.ticket_id = ticket_id


def encode(message: OutboundMessage, *, ts: datetime) -> str:
    """Serialize an outbound message as a JSON text frame. `ts` must be timezone-aware."""
    envelope = Envelope(
        type=message.message_type,
        ticket_id=message.ticket_id,
        ts=ts,
        payload=message.model_dump(mode="json", exclude={"ticket_id"}, exclude_none=True),
    )
    return envelope.model_dump_json()


def decode(frame: str | bytes) -> InboundMessage:
    """Parse an inbound frame. Raises `FrameError` and nothing else."""
    if isinstance(frame, bytes):
        raise FrameError(FrameErrorKind.BINARY, f"{len(frame)} bytes")  # ASSUMPTION: text frames only
    try:
        raw = json.loads(frame)
    except json.JSONDecodeError as exc:
        raise FrameError(FrameErrorKind.NOT_JSON, exc.msg) from None
    except RecursionError:
        raise FrameError(FrameErrorKind.NOT_JSON, "nesting too deep") from None
    try:
        envelope = Envelope.model_validate(raw)
    except ValidationError as exc:
        raise FrameError(FrameErrorKind.BAD_ENVELOPE, _describe(exc)) from None

    model = _INBOUND.get(envelope.type)
    if model is None:
        raise FrameError(
            FrameErrorKind.UNKNOWN_TYPE,
            "not an inbound message type",
            message_type=envelope.type,
            ticket_id=envelope.ticket_id,
        )
    try:
        return model.model_validate({**envelope.payload, "ticket_id": envelope.ticket_id, "ts": envelope.ts})
    except ValidationError as exc:
        raise FrameError(
            FrameErrorKind.BAD_PAYLOAD,
            _describe(exc),
            message_type=envelope.type,
            ticket_id=envelope.ticket_id,
        ) from None


def _describe(exc: ValidationError) -> str:
    # Field locations and messages only: input values stay out of logs.
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    return "; ".join(f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}" for err in errors)
