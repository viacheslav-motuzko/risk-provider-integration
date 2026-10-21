"""STUB wallet operations, idempotent per bet: one `WalletHold` row per bet, changed by conditional updates."""

from decimal import Decimal

from django.db import transaction
from django.db.models import F

from apps.wallets.models import HoldState, Wallet, WalletHold


class InsufficientFunds(Exception):
    pass


class HoldStateError(Exception):
    """A wallet invariant is broken: the hold is missing, or in a state the operation must not change."""


def fund(*, customer_ref: str, currency: str, amount: Decimal) -> None:
    """Credit a wallet, creating it if needed. For the demo and tests."""
    with transaction.atomic():
        wallet, _ = Wallet.objects.get_or_create(customer_ref=customer_ref, currency=currency, defaults={"balance": 0})
        Wallet.objects.filter(pk=wallet.pk).update(balance=F("balance") + amount)


def hold(*, customer_ref: str, currency: str, bet_id: int, amount: Decimal) -> None:
    """Take the stake off the balance and record the hold. Raises `InsufficientFunds`, also for a missing wallet."""
    with transaction.atomic():
        wallet = Wallet.objects.filter(customer_ref=customer_ref, currency=currency).only("pk").first()
        if wallet is None or not Wallet.objects.filter(pk=wallet.pk, balance__gte=amount).update(
            balance=F("balance") - amount
        ):
            raise InsufficientFunds(f"bet {bet_id}: no {currency} wallet with {amount} available")
        WalletHold.objects.create(bet_id=bet_id, wallet=wallet, amount=amount)


def capture(bet_id: int) -> None:
    """The stake becomes the bet's money. Idempotent; refuses a released or missing hold."""
    with transaction.atomic():
        if WalletHold.objects.filter(bet_id=bet_id, state=HoldState.HELD).update(state=HoldState.CAPTURED):
            return
        state = _state(bet_id)
        if state != HoldState.CAPTURED:
            raise HoldStateError(f"bet {bet_id}: cannot capture a hold in state {state}")


def release(bet_id: int) -> bool:
    """Return the stake to the balance; True if this call released it. Idempotent, and a missing hold has nothing
    to release. Never releases a captured stake: nothing refunds an accepted bet."""
    with transaction.atomic():
        if not WalletHold.objects.filter(bet_id=bet_id, state=HoldState.HELD).update(state=HoldState.RELEASED):
            if _state(bet_id) == HoldState.CAPTURED:
                raise HoldStateError(f"bet {bet_id}: the stake is captured")
            return False
        released = WalletHold.objects.values("wallet_id", "amount").get(bet_id=bet_id)
        Wallet.objects.filter(pk=released["wallet_id"]).update(balance=F("balance") + released["amount"])
        return True


def _state(bet_id: int) -> str | None:
    return WalletHold.objects.filter(bet_id=bet_id).values_list("state", flat=True).first()
