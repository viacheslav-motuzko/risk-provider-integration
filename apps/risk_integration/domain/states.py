"""Ticket states and the reasons persisted with them (`docs/kb/ticket-lifecycle.md`).

The reason enums share their spelling with the wire enums in `protocol` (A-16); the domain cannot import
`protocol`, so the runtime maps between them by value.
"""

from enum import StrEnum


class State(StrEnum):
    PENDING_SEND = "PENDING_SEND"
    SENT = "SENT"
    CONFIRM_PENDING = "CONFIRM_PENDING"
    CONFIRMED = "CONFIRMED"
    DECLINE_PENDING = "DECLINE_PENDING"
    DECLINED = "DECLINED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    ESCALATED = "ESCALATED"


class Decision(StrEnum):
    """The provider's recommendation as received."""

    ACCEPT = "ACCEPT"
    DECLINE = "DECLINE"


class CancelReason(StrEnum):
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    APPLY_FAILED = "APPLY_FAILED"
    TICKET_REJECTED = "TICKET_REJECTED"


class EscalationReason(StrEnum):
    CONFIRM_REJECTED = "CONFIRM_REJECTED"
    CANCEL_REJECTED = "CANCEL_REJECTED"


class MessageRef(StrEnum):
    """Which of our outbound messages a provider error refers to (A-02)."""

    TICKET = "TICKET"
    CONFIRM_RECOMMENDATION = "CONFIRM_RECOMMENDATION"
    DECLINE_RECOMMENDATION = "DECLINE_RECOMMENDATION"
    REQUEST_CANCELLATION = "REQUEST_CANCELLATION"


# Before acceptance: the stake is held and never captured, so only these states may compensate (D7).
AWAITING_RECOMMENDATION = frozenset({State.PENDING_SEND, State.SENT})
# A message is owed to the provider and the relay sends it on its normal path.
OUTBOX = frozenset({State.PENDING_SEND, State.CONFIRM_PENDING, State.DECLINE_PENDING, State.CANCEL_PENDING})
# The final message was sent; re-sent after a reconnect within the loss window (D11).
CLOSED_FINAL = frozenset({State.CONFIRMED, State.DECLINED, State.CANCELLED})
