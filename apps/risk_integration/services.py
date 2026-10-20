"""Use cases that apply a decision to the monolith and the ticket together.

Each runs as one `run_db` unit of work: lock the ticket in its source states, let the monolith decide and apply
through the port, then commit the conditional transition. The lock makes a duplicate or a racing
cancellation wait and then find nothing to do, so money moves once. The port does database work only.
"""

from uuid import UUID

from apps.risk_integration.domain.machine import Trigger
from apps.risk_integration.domain.states import Decision, State
from apps.risk_integration.persistence import tickets
from apps.risk_integration.ports import Accepted, BetDecisionPort, Rejected


class LockedTransitionMissed(RuntimeError):
    """A transition did not apply although the ticket was locked in a source state: a bug, never classified"""


def apply_recommendation(port: BetDecisionPort, ticket_id: UUID, decision: Decision) -> State | None:
    bet_id = tickets.lock_awaiting(ticket_id)
    if bet_id is None:
        return None
    if decision is Decision.DECLINE:
        port.decline(bet_id)
        _require(tickets.record_decline(ticket_id), ticket_id)
        return State.DECLINE_PENDING
    match port.accept(bet_id):
        case Accepted():
            _require(tickets.record_accept(ticket_id), ticket_id)
            return State.CONFIRM_PENDING
        case Rejected(reason):
            _require(tickets.record_domain_rejection(ticket_id, decline_reason=reason), ticket_id)
            return State.DECLINE_PENDING


def cancel_ticket(port: BetDecisionPort, ticket_id: UUID, trigger: Trigger, *, error_code: str = "") -> bool:
    bet_id = tickets.lock_awaiting(ticket_id, past_deadline=trigger is Trigger.DEADLINE_PASSED)
    if bet_id is None:
        return False
    port.void(bet_id)
    _require(tickets.cancel(ticket_id, trigger, error_code=error_code), ticket_id)
    return True


def _require(applied: bool, ticket_id: UUID) -> None:
    if not applied:
        raise LockedTransitionMissed(str(ticket_id))
