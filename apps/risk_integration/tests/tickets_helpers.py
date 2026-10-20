import itertools
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db.models.functions import Now

from apps.risk_integration.domain.states import State
from apps.risk_integration.models import RiskTicket
from apps.risk_integration.persistence.tickets import TicketSnapshot, create_ticket

LEASE = timedelta(seconds=10)
DEADLINE = timedelta(seconds=30)

_bet_ids = itertools.count(1)


def new_ticket(deadline: timedelta = DEADLINE) -> UUID:
    snapshot = TicketSnapshot(
        stake=Decimal("10.50"), currency="EUR", odds=Decimal("1.8500"), selection_id="sel-1", customer_ref="c-1"
    )
    return create_ticket(bet_id=next(_bet_ids), snapshot=snapshot, deadline=deadline)


def ago(seconds: float) -> Any:
    """A DB-time expression `seconds` in the past (negative: in the future). Tests move time this way, on DB time."""
    return Now() - timedelta(seconds=seconds)


def force(ticket_id: UUID, **values: Any) -> None:
    """Test setup only: write columns directly, bypassing the state machine."""
    RiskTicket.objects.filter(ticket_id=ticket_id).update(**values)


def row(ticket_id: UUID) -> RiskTicket:
    return RiskTicket.objects.get(ticket_id=ticket_id)


def state(ticket_id: UUID) -> State:
    return State(row(ticket_id).state)
