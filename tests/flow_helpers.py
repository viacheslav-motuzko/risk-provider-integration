"""Shared setup for tests that cross the monolith and the integration."""

from decimal import Decimal

from apps.betting.models import Bet
from apps.betting.services import PlacedBet, place_bet
from apps.risk_integration.adapters.monolith import DjangoBetDecisionAdapter
from apps.risk_integration.domain.states import State
from apps.risk_integration.services import Applied, Ignored
from apps.wallets import services as wallets
from apps.wallets.models import Wallet, WalletHold

CUSTOMER = "c-1"
CURRENCY = "EUR"
FUNDS = Decimal("100.00")
STAKE = Decimal("10.00")
ODDS = Decimal("1.8500")
ODDS_OVER_LIMIT = Decimal("1001.0000")  # STAKE x ODDS_OVER_LIMIT exceeds STUB_MAX_POTENTIAL_PAYOUT

PORT = DjangoBetDecisionAdapter()


def funded_bet(*, odds: Decimal = ODDS) -> PlacedBet:
    wallets.fund(customer_ref=CUSTOMER, currency=CURRENCY, amount=FUNDS)
    return place_bet(customer_ref=CUSTOMER, selection_id="sel-1", odds=odds, stake=STAKE, currency=CURRENCY)


def balance() -> Decimal:
    value: Decimal = Wallet.objects.get(customer_ref=CUSTOMER, currency=CURRENCY).balance
    return value


def bet_state(bet_id: int) -> str:
    return Bet.objects.get(pk=bet_id).state


def hold_state(bet_id: int) -> str | None:
    return WalletHold.objects.filter(bet_id=bet_id).values_list("state", flat=True).first()


def applied_state(applied: Applied | Ignored) -> State | Ignored:
    return applied.state if isinstance(applied, Applied) else applied
