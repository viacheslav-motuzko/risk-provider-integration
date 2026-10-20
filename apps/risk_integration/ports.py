"""The integration's only seam into the monolith. The Django adapter implements it"""

from dataclasses import dataclass
from typing import Protocol

from apps.risk_integration.domain.states import DeclineReason


@dataclass(frozen=True)
class Accepted:
    pass


@dataclass(frozen=True)
class Rejected:
    """Our own rules reject a bet the provider recommended to accept"""

    reason: DeclineReason


type AcceptOutcome = Accepted | Rejected


class BetDecisionPort(Protocol):
    """Applies a decision to the monolith. Every call runs inside the integration's unit of work, under the ticket's
    row lock, and commits together with the ticket transition. So an implementation does database work only: no
    network I/O and no waiting. It raises `PermanentApplyError` for a broken monolith invariant"""

    def accept(self, bet_id: int) -> AcceptOutcome:
        """Accept the bet and capture the stake, or reject it by our own rules and release the stake"""
        ...

    def decline(self, bet_id: int) -> None:
        """Reject the bet and release the stake"""
        ...

    def void(self, bet_id: int) -> None:
        """Compensation: void the bet and release the stake. Idempotent; refuses an accepted bet"""
        ...
