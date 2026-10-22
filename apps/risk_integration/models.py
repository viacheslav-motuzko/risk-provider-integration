import uuid
from collections.abc import Iterable
from enum import StrEnum

from django.db import models
from django.db.models.functions import Now

from apps.risk_integration.domain.states import (
    AWAITING_RECOMMENDATION,
    CLOSED_FINAL,
    OUTBOX,
    CancelReason,
    Decision,
    EscalationReason,
    MessageRef,
    State,
)


def _choices(enum: type[StrEnum]) -> list[tuple[str, str]]:
    return [(m.value, m.value) for m in enum]


# Plain sorted strings: migrations must not reference domain enums, and must be stable.
def _in(states: Iterable[State]) -> list[str]:
    return sorted(s.value for s in states)


class RiskTicket(models.Model):
    """One bet submitted to the provider; the outbox row for every message about it."""

    ticket_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # No foreign key: the integration stays cheap to cut from the monolith app.
    bet_id = models.BigIntegerField(unique=True)
    state = models.CharField(max_length=16, choices=_choices(State), default=State.PENDING_SEND.value)

    # The TICKET payload, snapshotted at creation so a retransmission sends identical content.
    stake = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3)
    odds = models.DecimalField(max_digits=12, decimal_places=4)
    selection_id = models.CharField(max_length=64)
    customer_ref = models.CharField(max_length=64)

    created_at = models.DateTimeField(db_default=Now())
    deadline_at = models.DateTimeField()
    retransmit_at = models.DateTimeField(null=True)
    last_sent_at = models.DateTimeField(null=True)
    final_resends = models.PositiveSmallIntegerField(default=0, db_default=0)

    locked_by = models.CharField(max_length=64, default="")
    locked_until = models.DateTimeField(null=True)

    recommendation = models.CharField(max_length=8, choices=_choices(Decision), default="")
    decline_reason = models.CharField(max_length=32, default="")  # sent with our decline
    cancel_reason = models.CharField(max_length=32, choices=_choices(CancelReason), default="")
    last_error_code = models.TextField(default="")  # raw provider code
    last_error_ref_type = models.CharField(max_length=32, choices=_choices(MessageRef), default="")
    escalation_reason = models.CharField(max_length=32, choices=_choices(EscalationReason), default="")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(state__in=_in(State)), name="risk_ticket_state_valid"),
            # A cancellation re-sent after a reconnect must carry its reason from the row.
            models.CheckConstraint(
                condition=~models.Q(state__in=_in([State.CANCEL_PENDING, State.CANCELLED]))
                | ~models.Q(cancel_reason=""),
                name="risk_ticket_cancel_has_reason",
            ),
        ]
        indexes = [
            # claim_due: messages owed to the provider.
            models.Index(fields=["created_at"], condition=models.Q(state__in=_in(OUTBOX)), name="risk_ticket_outbox"),
            # claim_due: SENT tickets at their retransmit point.
            models.Index(
                fields=["retransmit_at"],
                condition=models.Q(state=State.SENT.value, retransmit_at__isnull=False),
                name="risk_ticket_retransmit",
            ),
            # find_expired and claim_sent_for_resend: tickets waiting for a recommendation.
            models.Index(
                fields=["deadline_at"],
                condition=models.Q(state__in=_in(AWAITING_RECOMMENDATION)),
                name="risk_ticket_awaiting",
            ),
            # claim_finals_for_resend: final messages within the loss window.
            models.Index(
                fields=["last_sent_at"], condition=models.Q(state__in=_in(CLOSED_FINAL)), name="risk_ticket_closed"
            ),
        ]
