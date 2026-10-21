"""STUB bet use cases. Each owns its `atomic`, including the wallet effect; called through the risk
integration's adapter inside a `run_db` unit of work, it is a savepoint of that unit."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from django.db import transaction

from apps.betting.models import Bet, BetState
from apps.risk_integration.api import TicketSnapshot, enqueue_ticket
from apps.wallets import services as wallets

# STUB: stands in for the monolith's real limits, which depend on exposure at decision time, so they are checked
# when the bet is accepted, not when it is placed.
STUB_MAX_POTENTIAL_PAYOUT = Decimal("10000.00")


class Rejection(StrEnum):
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"


class BetStateError(Exception):
    """A bet invariant is broken: the bet is missing, or in a state the operation must not change."""


@dataclass(frozen=True)
class PlacedBet:
    bet_id: int
    ticket_id: UUID


def place_bet(*, customer_ref: str, selection_id: str, odds: Decimal, stake: Decimal, currency: str) -> PlacedBet:
    """The bet, its held stake and its risk ticket commit together or not at all. Raises `InsufficientFunds`."""
    with transaction.atomic():
        bet = Bet.objects.create(
            customer_ref=customer_ref, selection_id=selection_id, odds=odds, stake=stake, currency=currency
        )
        wallets.hold(customer_ref=customer_ref, currency=currency, bet_id=bet.pk, amount=stake)
        snapshot = TicketSnapshot(
            stake=stake, currency=currency, odds=odds, selection_id=selection_id, customer_ref=customer_ref
        )
        ticket_id = enqueue_ticket(bet_id=bet.pk, snapshot=snapshot)
    return PlacedBet(bet_id=bet.pk, ticket_id=ticket_id)


def accept_bet(bet_id: int) -> Rejection | None:
    """Accept the bet and capture the stake; or, if our own limits reject it, reject it and release the stake."""
    with transaction.atomic():
        bet = Bet.objects.filter(pk=bet_id).only("stake", "odds").first()
        if bet is not None and bet.stake * bet.odds > STUB_MAX_POTENTIAL_PAYOUT:
            _change_state(bet_id, BetState.REJECTED)
            wallets.release(bet_id)
            return Rejection.LIMIT_EXCEEDED
        _change_state(bet_id, BetState.ACCEPTED)
        wallets.capture(bet_id)
        return None


def reject_bet(bet_id: int) -> None:
    with transaction.atomic():
        _change_state(bet_id, BetState.REJECTED)
        wallets.release(bet_id)


def void_bet(bet_id: int) -> None:
    """Compensation for a failed placement. Idempotent: a bet already voided or rejected stays as it is.
    Refuses an accepted bet: nothing refunds a captured stake."""
    with transaction.atomic():
        voided = Bet.objects.filter(pk=bet_id, state=BetState.PENDING_RISK).update(state=BetState.VOIDED)
        if not voided:
            state = Bet.objects.filter(pk=bet_id).values_list("state", flat=True).first()
            if state not in (BetState.VOIDED, BetState.REJECTED):
                raise BetStateError(f"bet {bet_id}: cannot void a bet in state {state}")
        wallets.release(bet_id)


def _change_state(bet_id: int, to: BetState) -> None:
    if not Bet.objects.filter(pk=bet_id, state=BetState.PENDING_RISK).update(state=to):
        state = Bet.objects.filter(pk=bet_id).values_list("state", flat=True).first()
        raise BetStateError(f"bet {bet_id}: cannot move from {state} to {to}")
