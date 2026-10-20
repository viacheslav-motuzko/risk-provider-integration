from decimal import Decimal

import pytest
from django.db import transaction

from apps.wallets import services as wallets
from apps.wallets.models import HoldState, Wallet, WalletHold

pytestmark = pytest.mark.django_db

AMOUNT = Decimal("10.00")


def _held(bet_id: int = 1) -> int:
    wallets.fund(customer_ref="c-1", currency="EUR", amount=AMOUNT)
    wallets.hold(customer_ref="c-1", currency="EUR", bet_id=bet_id, amount=AMOUNT)
    return bet_id


def _balance() -> Decimal:
    value: Decimal = Wallet.objects.get(customer_ref="c-1", currency="EUR").balance
    return value


def test_hold_takes_the_stake_off_the_balance():
    bet_id = _held()

    assert _balance() == 0
    assert WalletHold.objects.get(bet_id=bet_id).state == HoldState.HELD


def test_hold_refuses_an_overdraft_and_leaves_the_balance():
    wallets.fund(customer_ref="c-1", currency="EUR", amount=AMOUNT)

    with pytest.raises(wallets.InsufficientFunds):
        wallets.hold(customer_ref="c-1", currency="EUR", bet_id=1, amount=AMOUNT + Decimal("0.01"))

    assert _balance() == AMOUNT
    assert not WalletHold.objects.exists()


def test_release_returns_the_stake_once():
    bet_id = _held()

    assert wallets.release(bet_id)
    assert not wallets.release(bet_id)

    assert _balance() == AMOUNT


def test_capture_is_idempotent_and_moves_no_balance():
    bet_id = _held()

    wallets.capture(bet_id)
    wallets.capture(bet_id)

    assert (WalletHold.objects.get(bet_id=bet_id).state, _balance()) == (HoldState.CAPTURED, Decimal(0))


def test_a_captured_stake_is_never_released():
    bet_id = _held()
    wallets.capture(bet_id)

    with pytest.raises(wallets.HoldStateError), transaction.atomic():
        wallets.release(bet_id)

    assert (WalletHold.objects.get(bet_id=bet_id).state, _balance()) == (HoldState.CAPTURED, Decimal(0))


@pytest.mark.parametrize("released", [True, False])  # False: no hold at all
def test_capture_refuses_a_hold_that_is_not_held(released):
    if released:
        wallets.release(_held())

    with pytest.raises(wallets.HoldStateError), transaction.atomic():
        wallets.capture(1)


def test_releasing_a_missing_hold_has_nothing_to_release():
    assert not wallets.release(1)
