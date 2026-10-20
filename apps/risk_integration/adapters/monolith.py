"""`BetDecisionPort` over the monolith's services"""

from collections.abc import Iterator
from contextlib import contextmanager
from types import MappingProxyType

from apps.betting import services as betting
from apps.risk_integration.domain.errors import PermanentApplyError
from apps.risk_integration.domain.states import DeclineReason
from apps.risk_integration.ports import Accepted, AcceptOutcome, Rejected
from apps.wallets.services import HoldStateError

_DECLINE_REASONS = MappingProxyType({betting.Rejection.LIMIT_EXCEEDED: DeclineReason.LIMIT_EXCEEDED})


class DjangoBetDecisionAdapter:
    def accept(self, bet_id: int) -> AcceptOutcome:
        with _permanent():
            rejection = betting.accept_bet(bet_id)
        return Accepted() if rejection is None else Rejected(_DECLINE_REASONS[rejection])

    def decline(self, bet_id: int) -> None:
        with _permanent():
            betting.reject_bet(bet_id)

    def void(self, bet_id: int) -> None:
        with _permanent():
            betting.void_bet(bet_id)


@contextmanager
def _permanent() -> Iterator[None]:
    try:
        yield
    except (betting.BetStateError, HoldStateError) as exc:
        raise PermanentApplyError(str(exc)) from exc
