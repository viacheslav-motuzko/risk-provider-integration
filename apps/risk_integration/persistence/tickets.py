"""Ticket persistence. Every function runs inside the caller's transaction: one `run_db` unit of work, or the
monolith's `atomic` for `create_ticket`. Claims hold row locks until that transaction commits, so a claim is a
unit of work of its own.

Transitions are conditional updates built from `domain.machine.RULES`; each clears the lease and returns whether
it applied. A miss (duplicate, late reply, lost lease) is a normal outcome for the caller to log and count.
Monolith side effects (stage 3) run in the same transaction after a transition that applied.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import connection, transaction
from django.db.models import DateTimeField, DurationField, ExpressionWrapper, Q, Value
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
    OUTBOX,
    CancelReason,
    Decision,
    State,
)
from apps.risk_integration.models import RiskTicket


@dataclass(frozen=True)
class TicketSnapshot:
    """ASSUMPTION A-16: the single-bet TICKET payload, built by the monolith when the bet is placed."""

    stake: Decimal
    currency: str
    odds: Decimal
    selection_id: str
    customer_ref: str


@dataclass(frozen=True)
class ClaimedTicket:
    """A row the relay holds a lease on, with everything needed to build the message owed in `state`."""

    ticket_id: UUID
    bet_id: int
    state: State
    snapshot: TicketSnapshot
    decline_reason: str
    cancel_reason: CancelReason | None


def create_ticket(*, bet_id: int, snapshot: TicketSnapshot, deadline: timedelta) -> UUID:
    """Row 1. Must run in the transaction that creates the bet, so neither exists without the other (D2)."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("create_ticket must run inside the transaction that creates the bet")
    ticket = RiskTicket.objects.create(
        bet_id=bet_id,
        stake=snapshot.stake,
        currency=snapshot.currency,
        odds=snapshot.odds,
        selection_id=snapshot.selection_id,
        customer_ref=snapshot.customer_ref,
        deadline_at=_db_now_plus(deadline),  # D11: the budget counts from creation
    )
    return ticket.ticket_id


def claim_due(*, owner: str, lease: timedelta, limit: int) -> list[ClaimedTicket]:
    """The relay's normal pass: every message owed (OUTBOX) and SENT tickets at their retransmit point (D11)."""
    owed = Q(state__in=OUTBOX - {State.PENDING_SEND}) | _before_deadline(State.PENDING_SEND)
    retransmit = _before_deadline(State.SENT) & Q(retransmit_at__lte=Now())
    return _claim(owed | retransmit, owner=owner, lease=lease, limit=limit)


def claim_sent_for_resend(*, owner: str, lease: timedelta, limit: int, since: datetime) -> list[ClaimedTicket]:
    """After a (re)connect at DB time `since`: SENT tickets not yet re-sent on this session (A-07, row 3).
    A re-send moves `last_sent_at` past `since`, so calling this until it returns nothing pages through all."""
    return _claim(_before_deadline(State.SENT) & Q(last_sent_at__lt=since), owner=owner, lease=lease, limit=limit)


def claim_finals_for_resend(
    *, owner: str, lease: timedelta, limit: int, window_start: datetime, since: datetime
) -> list[ClaimedTicket]:
    """After a (re)connect at DB time `since`: final messages sent in `[window_start, since)`, whatever their
    type (D11). Pages like `claim_sent_for_resend`."""
    due = Q(state__in=CLOSED_FINAL, last_sent_at__gte=window_start, last_sent_at__lt=since)
    return _claim(due, owner=owner, lease=lease, limit=limit)


def record_send(ticket_id: UUID, sent_from: State, *, owner: str, retransmit_after: timedelta) -> bool:
    """Rows 2, 3, 11-13 and the final re-send: the relay sent the message owed in `sent_from` and still holds
    the lease. A crash before this commit makes the relay send again (A-03)."""
    trigger = send_trigger(sent_from)
    values: dict[str, Any] = {"last_sent_at": Now()}
    if trigger is Trigger.TICKET_SENT:
        values["retransmit_at"] = _db_now_plus(retransmit_after)
    elif trigger is Trigger.TICKET_RESENT:
        values["retransmit_at"] = None
    return _transition(ticket_id, trigger, owner=owner, **values)


def record_accept(ticket_id: UUID) -> bool:
    """Row 4. The caller captures the stake in the same transaction when this returns True."""
    return _transition(ticket_id, Trigger.ACCEPT_APPLIED, recommendation=Decision.ACCEPT)


def record_decline(ticket_id: UUID) -> bool:
    """Row 5. The caller rejects the bet and releases the stake in the same transaction when this returns True."""
    return _transition(ticket_id, Trigger.DECLINE_APPLIED, recommendation=Decision.DECLINE)


def record_domain_rejection(ticket_id: UUID, *, decline_reason: str) -> bool:
    """Row 6: ACCEPT, but our own rules reject the bet (A-08); we decline with `decline_reason`."""
    return _transition(
        ticket_id, Trigger.DOMAIN_REJECTED, recommendation=Decision.ACCEPT, decline_reason=decline_reason
    )


def cancel(ticket_id: UUID, trigger: Trigger, *, error_code: str = "") -> bool:
    """Rows 8-10. The caller compensates in the same transaction when this returns True (D7)."""
    values: dict[str, Any] = {"cancel_reason": CANCEL_REASONS[trigger]}
    if trigger in ERROR_REFS:
        values.update(last_error_code=error_code, last_error_ref_type=ERROR_REFS[trigger])
    extra = Q(deadline_at__lte=Now()) if trigger is Trigger.DEADLINE_PASSED else None
    return _transition(ticket_id, trigger, extra=extra, **values)


def escalate(ticket_id: UUID, trigger: Trigger, *, error_code: str) -> bool:
    """Rows 14-15: the provider refused our confirm (B-01) or cancellation (B-03). No money moves."""
    return _transition(
        ticket_id,
        trigger,
        escalation_reason=ESCALATION_REASONS[trigger],
        last_error_code=error_code,
        last_error_ref_type=ERROR_REFS[trigger],
    )


def find_expired(*, limit: int) -> list[UUID]:
    """Tickets still waiting for a recommendation at their deadline (row 9), for the sweeper."""
    due = Q(state__in=AWAITING_RECOMMENDATION, deadline_at__lte=Now())
    return list(RiskTicket.objects.filter(due).order_by("deadline_at").values_list("ticket_id", flat=True)[:limit])


def db_now() -> datetime:
    """DB time, for anchors the runtime keeps between units of work (the connect time of a session, D12)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT statement_timestamp()")  # the clock of Django's Now() on PostgreSQL
        (value,) = cursor.fetchone()
    now: datetime = value
    return now


def _claim(due: Q, *, owner: str, lease: timedelta, limit: int) -> list[ClaimedTicket]:
    # D3: no FIFO guarantee with SKIP LOCKED; the order is best-effort.
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
