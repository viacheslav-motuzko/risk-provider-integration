"""Typed messages. Envelope fields (`ticket_id`, `ts`) are flattened into each message; the codec maps them."""

from decimal import Decimal
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class OutboundType(StrEnum):
    TICKET = "TICKET"
    CONFIRM_RECOMMENDATION = "CONFIRM_RECOMMENDATION"
    DECLINE_RECOMMENDATION = "DECLINE_RECOMMENDATION"
    REQUEST_CANCELLATION = "REQUEST_CANCELLATION"


class InboundType(StrEnum):
    RECOMMENDATION = "RECOMMENDATION"  # ASSUMPTION A-01
    ERROR = "ERROR"  # ASSUMPTION A-02


class Decision(StrEnum):
    ACCEPT = "ACCEPT"
    DECLINE = "DECLINE"


class DeclineReason(StrEnum):
    """ASSUMPTION A-08: why we decline, including after an ACCEPT recommendation."""

    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    BET_NOT_PLACEABLE = "BET_NOT_PLACEABLE"
    OTHER = "OTHER"


class CancellationReason(StrEnum):
    """ASSUMPTION A-16: why our placement flow failed (D7)."""

    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    APPLY_FAILED = "APPLY_FAILED"
    TICKET_REJECTED = "TICKET_REJECTED"


# ASSUMPTION A-09: the only error code whose meaning changes our reaction; every other code is kept as received.
UNKNOWN_TICKET_CODE = "UNKNOWN_TICKET"


class _Outbound(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_type: ClassVar[OutboundType]

    ticket_id: UUID


class Ticket(_Outbound):
    """ASSUMPTION A-16: one single bet. Stake and decimal odds travel as decimal strings; `selection_id` is the
    provider's outcome id and `customer_ref` a pseudonymous reference, both opaque to us. Business validation
    belongs to the monolith; this model only keeps money exact."""

    message_type = OutboundType.TICKET

    stake: Decimal = Field(allow_inf_nan=False)
    currency: str
    odds: Decimal = Field(allow_inf_nan=False)
    selection_id: str
    customer_ref: str


class ConfirmRecommendation(_Outbound):
    message_type = OutboundType.CONFIRM_RECOMMENDATION


class DeclineRecommendation(_Outbound):
    message_type = OutboundType.DECLINE_RECOMMENDATION

    reason: DeclineReason | None = None


class RequestCancellation(_Outbound):
    message_type = OutboundType.REQUEST_CANCELLATION

    reason: CancellationReason


class _Inbound(BaseModel):
    # Unknown fields are ignored so the provider can add fields without breaking decoding.
    model_config = ConfigDict(frozen=True, extra="ignore")

    message_type: ClassVar[InboundType]

    ts: AwareDatetime | None = None


class Recommendation(_Inbound):
    message_type = InboundType.RECOMMENDATION

    ticket_id: UUID
    decision: Decision
    reason: str | None = None


class ProviderError(_Inbound):
    """ASSUMPTION A-02: `ref_type` names which of our messages is rejected, so the error can be correlated
    after the ticket has moved on. `code` is kept raw for logs and the ticket row."""

    message_type = InboundType.ERROR

    ticket_id: UUID | None = None
    ref_type: OutboundType | None = None
    code: str = Field(min_length=1)
    message: str = ""

    @property
    def is_unknown_ticket(self) -> bool:
        return self.code == UNKNOWN_TICKET_CODE


type OutboundMessage = Ticket | ConfirmRecommendation | DeclineRecommendation | RequestCancellation
type InboundMessage = Recommendation | ProviderError
