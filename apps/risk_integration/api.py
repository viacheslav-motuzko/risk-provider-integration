"""The only module of the risk integration the monolith imports."""

from uuid import UUID

from django.conf import settings

from apps.risk_integration.persistence.tickets import TicketSnapshot, create_ticket

__all__ = ["TicketSnapshot", "enqueue_ticket"]


def enqueue_ticket(*, bet_id: int, snapshot: TicketSnapshot) -> UUID:
    return create_ticket(bet_id=bet_id, snapshot=snapshot, deadline=settings.RISK_TICKET_DEADLINE)
