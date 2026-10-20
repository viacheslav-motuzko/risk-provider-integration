"""The monolith side of the flow, one unit of work at a time: placement, decisions and compensation, with the
money they move. A unit that fails runs in a savepoint here; the full `run_db` path is in the concurrency tests."""

import json
from decimal import Decimal
from io import StringIO

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import OperationalError, transaction

from apps.betting.models import Bet, BetState
from apps.betting.services import place_bet
from apps.risk_integration import api, services
from apps.risk_integration.domain.errors import PermanentApplyError
from apps.risk_integration.domain.machine import RULES, Trigger
from apps.risk_integration.domain.states import (
    AWAITING_RECOMMENDATION,
    CancelReason,
    Decision,
    DeclineReason,
    MessageRef,
    State,
)
from apps.risk_integration.models import RiskTicket
from apps.risk_integration.persistence import tickets
from apps.risk_integration.persistence.errors import ApplyFailure, classify_apply_failure
from apps.risk_integration.tests.tickets_helpers import ago, force, row
from apps.wallets import services as wallets
from apps.wallets.models import HoldState, WalletHold
from apps.wallets.services import InsufficientFunds
from tests.flow_helpers import (
    CURRENCY,
    CUSTOMER,
    FUNDS,
    ODDS,
    ODDS_OVER_LIMIT,
    PORT,
    STAKE,
    balance,
    bet_state,
    funded_bet,
    hold_state,
)

pytestmark = pytest.mark.django_db

AWAITING = [State.PENDING_SEND, State.SENT]


def test_place_bet_commits_the_bet_the_held_stake_and_the_pending_ticket_together():
    placed = funded_bet()

    ticket = row(placed.ticket_id)
    assert bet_state(placed.bet_id) == BetState.PENDING_RISK
    assert hold_state(placed.bet_id) == HoldState.HELD
    assert balance() == FUNDS - STAKE
    assert (ticket.state, ticket.bet_id, ticket.stake) == (State.PENDING_SEND, placed.bet_id, STAKE)
    assert ticket.deadline_at - ticket.created_at == settings.RISK_TICKET_DEADLINE


def test_place_bet_leaves_nothing_behind_when_the_ticket_cannot_be_created(monkeypatch):
    def fail(**_):
        raise OperationalError("simulated")

    monkeypatch.setattr(api, "create_ticket", fail)

    with pytest.raises(OperationalError):
        funded_bet()

    assert not Bet.objects.exists()
    assert not WalletHold.objects.exists()
    assert balance() == FUNDS


@pytest.mark.parametrize("funds", [STAKE - Decimal("0.01"), None])  # None: no wallet at all
def test_place_bet_without_enough_money_creates_nothing(funds):
    if funds is not None:
        wallets.fund(customer_ref=CUSTOMER, currency=CURRENCY, amount=funds)

    with pytest.raises(InsufficientFunds):
        place_bet(customer_ref=CUSTOMER, selection_id="sel-1", odds=ODDS, stake=STAKE, currency=CURRENCY)

    assert not Bet.objects.exists()
    assert not WalletHold.objects.exists()


@pytest.mark.parametrize("start", AWAITING)  # PENDING_SEND: the reply beat the relay's commit
def test_accept_captures_the_stake_and_waits_to_confirm(start):
    placed = funded_bet()
    force(placed.ticket_id, state=start)

    assert services.apply_recommendation(PORT, placed.ticket_id, Decision.ACCEPT) == State.CONFIRM_PENDING

    assert (row(placed.ticket_id).state, row(placed.ticket_id).recommendation) == (State.CONFIRM_PENDING, "ACCEPT")
    assert (bet_state(placed.bet_id), hold_state(placed.bet_id)) == (BetState.ACCEPTED, HoldState.CAPTURED)
    assert balance() == FUNDS - STAKE


@pytest.mark.parametrize("start", AWAITING)
def test_decline_rejects_the_bet_and_releases_the_stake(start):
    placed = funded_bet()
    force(placed.ticket_id, state=start)

    assert services.apply_recommendation(PORT, placed.ticket_id, Decision.DECLINE) == State.DECLINE_PENDING

    assert (row(placed.ticket_id).state, row(placed.ticket_id).recommendation) == (State.DECLINE_PENDING, "DECLINE")
    assert (bet_state(placed.bet_id), hold_state(placed.bet_id)) == (BetState.REJECTED, HoldState.RELEASED)
    assert balance() == FUNDS


def test_accept_over_our_limit_is_declined_and_releases_the_stake():
    placed = funded_bet(odds=ODDS_OVER_LIMIT)

    assert services.apply_recommendation(PORT, placed.ticket_id, Decision.ACCEPT) == State.DECLINE_PENDING

    ticket = row(placed.ticket_id)
    assert (ticket.state, ticket.recommendation, ticket.decline_reason) == (
        State.DECLINE_PENDING,
        "ACCEPT",
        DeclineReason.LIMIT_EXCEEDED,
    )
    assert (bet_state(placed.bet_id), hold_state(placed.bet_id)) == (BetState.REJECTED, HoldState.RELEASED)
    assert balance() == FUNDS


@pytest.mark.parametrize("decision", list(Decision))
def test_duplicate_recommendation_is_ignored_and_moves_no_money(decision):
    placed = funded_bet()
    services.apply_recommendation(PORT, placed.ticket_id, decision)
    after_first = (row(placed.ticket_id).state, bet_state(placed.bet_id), hold_state(placed.bet_id), balance())

    assert services.apply_recommendation(PORT, placed.ticket_id, decision) is None

    assert (row(placed.ticket_id).state, bet_state(placed.bet_id), hold_state(placed.bet_id), balance()) == after_first


@pytest.mark.parametrize("start", AWAITING)
def test_deadline_cancellation_compensates_in_the_same_transaction(start):
    placed = funded_bet()
    force(placed.ticket_id, state=start)

    assert not services.cancel_ticket(PORT, placed.ticket_id, Trigger.DEADLINE_PASSED)  # not due yet
    assert (row(placed.ticket_id).state, bet_state(placed.bet_id), balance()) == (
        start,
        BetState.PENDING_RISK,
        FUNDS - STAKE,
    )

    force(placed.ticket_id, deadline_at=ago(1))
    assert services.cancel_ticket(PORT, placed.ticket_id, Trigger.DEADLINE_PASSED)

    ticket = row(placed.ticket_id)
    assert (ticket.state, ticket.cancel_reason) == (State.CANCEL_PENDING, CancelReason.DEADLINE_EXCEEDED)
    assert (bet_state(placed.bet_id), hold_state(placed.bet_id)) == (BetState.VOIDED, HoldState.RELEASED)
    assert balance() == FUNDS


def test_recommendation_after_the_cancellation_is_ignored():
    placed = funded_bet()
    force(placed.ticket_id, state=State.SENT, deadline_at=ago(1))
    services.cancel_ticket(PORT, placed.ticket_id, Trigger.DEADLINE_PASSED)

    assert services.apply_recommendation(PORT, placed.ticket_id, Decision.ACCEPT) is None

    assert (row(placed.ticket_id).state, bet_state(placed.bet_id)) == (State.CANCEL_PENDING, BetState.VOIDED)
    assert balance() == FUNDS


def test_refused_ticket_is_cancelled_with_compensation_and_keeps_the_error():
    placed = funded_bet()

    assert services.cancel_ticket(PORT, placed.ticket_id, Trigger.TICKET_REFUSED, error_code="MARKET_CLOSED")

    ticket = row(placed.ticket_id)
    assert (ticket.state, ticket.cancel_reason, ticket.last_error_code, ticket.last_error_ref_type) == (
        State.CANCEL_PENDING,
        CancelReason.TICKET_REJECTED,
        "MARKET_CLOSED",
        MessageRef.TICKET,
    )
    assert (bet_state(placed.bet_id), balance()) == (BetState.VOIDED, FUNDS)


def test_cancellation_after_acceptance_is_ignored_and_refunds_nothing():
    placed = funded_bet()
    services.apply_recommendation(PORT, placed.ticket_id, Decision.ACCEPT)

    assert not services.cancel_ticket(PORT, placed.ticket_id, Trigger.APPLY_FAILED)

    assert (row(placed.ticket_id).state, bet_state(placed.bet_id)) == (State.CONFIRM_PENDING, BetState.ACCEPTED)
    assert (hold_state(placed.bet_id), balance()) == (HoldState.CAPTURED, FUNDS - STAKE)


# Two guards: the bet refuses to be voided once accepted, and the wallet refuses to release a captured stake.
# Forcing the hold back to HELD takes the wallet's guard away, so the bet's guard is tested on its own.
@pytest.mark.parametrize("hold_after_accept", [HoldState.CAPTURED, HoldState.HELD])
def test_compensation_refuses_to_void_an_accepted_bet(hold_after_accept):
    placed = funded_bet()
    services.apply_recommendation(PORT, placed.ticket_id, Decision.ACCEPT)
    WalletHold.objects.filter(bet_id=placed.bet_id).update(state=hold_after_accept)

    with pytest.raises(PermanentApplyError), transaction.atomic():
        PORT.void(placed.bet_id)

    assert (bet_state(placed.bet_id), hold_state(placed.bet_id)) == (BetState.ACCEPTED, hold_after_accept)
    assert balance() == FUNDS - STAKE


def _release_hold_behind_the_bets_back(bet_id: int) -> None:
    WalletHold.objects.filter(bet_id=bet_id).update(state=HoldState.RELEASED)


def _delete_hold(bet_id: int) -> None:
    WalletHold.objects.filter(bet_id=bet_id).delete()


@pytest.mark.parametrize("break_invariant", [_release_hold_behind_the_bets_back, _delete_hold])
def test_broken_monolith_invariant_is_permanent_and_compensation_still_completes(break_invariant):
    placed = funded_bet()
    force(placed.ticket_id, state=State.SENT)
    break_invariant(placed.bet_id)

    with pytest.raises(PermanentApplyError) as failed, transaction.atomic():
        services.apply_recommendation(PORT, placed.ticket_id, Decision.ACCEPT)
    assert classify_apply_failure(failed.value) is ApplyFailure.PERMANENT
    assert (row(placed.ticket_id).state, bet_state(placed.bet_id)) == (State.SENT, BetState.PENDING_RISK)

    # The cancellation is a new unit of work after the failed one rolled back.
    assert services.cancel_ticket(PORT, placed.ticket_id, Trigger.APPLY_FAILED)

    ticket = row(placed.ticket_id)
    assert (ticket.state, ticket.cancel_reason) == (State.CANCEL_PENDING, CancelReason.APPLY_FAILED)
    assert bet_state(placed.bet_id) == BetState.VOIDED


def test_a_failed_transition_rolls_back_the_monolith_effect(monkeypatch):
    placed = funded_bet()
    force(placed.ticket_id, state=State.SENT)

    def fail(_):
        raise OperationalError("simulated")

    monkeypatch.setattr(tickets, "record_accept", fail)

    with pytest.raises(OperationalError) as failed, transaction.atomic():
        services.apply_recommendation(PORT, placed.ticket_id, Decision.ACCEPT)

    # Transient: the ticket stays SENT for the retransmission to bring the recommendation back.
    assert classify_apply_failure(failed.value) is ApplyFailure.TRANSIENT
    assert (row(placed.ticket_id).state, bet_state(placed.bet_id)) == (State.SENT, BetState.PENDING_RISK)
    assert (hold_state(placed.bet_id), balance()) == (HoldState.HELD, FUNDS - STAKE)


def test_demo_command_places_funded_bets_each_with_a_pending_ticket():
    out = StringIO()

    call_command("place_demo_bet", "--count", "3", stdout=out)

    placed = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(placed) == 3
    assert set(RiskTicket.objects.values_list("state", flat=True)) == {State.PENDING_SEND}
    assert set(Bet.objects.values_list("state", flat=True)) == {BetState.PENDING_RISK}


@pytest.mark.parametrize(
    "trigger",
    [
        Trigger.ACCEPT_APPLIED,
        Trigger.DECLINE_APPLIED,
        Trigger.DOMAIN_REJECTED,
        Trigger.APPLY_FAILED,
        Trigger.DEADLINE_PASSED,
        Trigger.TICKET_REFUSED,
    ],
)
def test_the_decision_lock_takes_exactly_the_source_states_of_its_transitions(trigger):
    # If they drift apart, a transition misses under the lock after the monolith effect ran.
    assert RULES[trigger].sources == AWAITING_RECOMMENDATION


def test_a_transition_that_misses_under_the_lock_fails_fast_and_rolls_back_the_monolith_effect(monkeypatch):
    placed = funded_bet()
    monkeypatch.setattr(tickets, "record_accept", lambda _: False)  # the lock and the table disagree: a bug

    with pytest.raises(services.LockedTransitionMissed) as failed, transaction.atomic():
        services.apply_recommendation(PORT, placed.ticket_id, Decision.ACCEPT)

    assert classify_apply_failure(failed.value) is None
    assert (bet_state(placed.bet_id), hold_state(placed.bet_id)) == (BetState.PENDING_RISK, HoldState.HELD)
