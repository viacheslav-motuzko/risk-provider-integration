"""Real threads and the real `run_db`, each with its own connection and transaction, against the real database."""

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
from django.db import OperationalError, connection, transaction

from apps.betting.models import BetState
from apps.risk_integration import services
from apps.risk_integration.domain.machine import Trigger
from apps.risk_integration.domain.states import Decision, State
from apps.risk_integration.persistence import tickets
from apps.risk_integration.persistence.db import DbRunner
from apps.risk_integration.ports import AcceptOutcome, BetDecisionPort
from apps.risk_integration.tests.tickets_helpers import force, row
from apps.wallets.models import HoldState
from tests.flow_helpers import FUNDS, PORT, STAKE, applied_state, balance, bet_state, funded_bet, hold_state

pytestmark = pytest.mark.django_db(transaction=True)

WAIT = 5  # seconds; only bounds a hang, never waited out in a passing run


def test_duplicate_recommendation_applied_by_two_appliers_at_once_moves_money_once():
    placed = funded_bet()

    first, second = _race(
        lambda port: applied_state(services.apply_recommendation(port, placed.ticket_id, Decision.DECLINE)),
        lambda port: applied_state(services.apply_recommendation(port, placed.ticket_id, Decision.DECLINE)),
    )

    assert (first, second) == (State.DECLINE_PENDING, services.Ignored.NOT_AWAITING)
    assert (hold_state(placed.bet_id), balance()) == (HoldState.RELEASED, FUNDS)


def test_cancellation_that_wins_the_race_with_an_accept_leaves_the_stake_released():
    placed = funded_bet()
    force(placed.ticket_id, state=State.SENT)

    cancelled, accepted = _race(
        lambda port: services.cancel_ticket(port, placed.ticket_id, Trigger.TICKET_REFUSED),
        lambda port: applied_state(services.apply_recommendation(port, placed.ticket_id, Decision.ACCEPT)),
    )

    assert (cancelled, accepted) == (True, services.Ignored.NOT_AWAITING)
    assert (row(placed.ticket_id).state, bet_state(placed.bet_id)) == (State.CANCEL_PENDING, BetState.VOIDED)
    assert (hold_state(placed.bet_id), balance()) == (HoldState.RELEASED, FUNDS)


def test_accept_that_wins_the_race_with_a_cancellation_keeps_the_stake_captured():
    placed = funded_bet()
    force(placed.ticket_id, state=State.SENT)

    accepted, cancelled = _race(
        lambda port: applied_state(services.apply_recommendation(port, placed.ticket_id, Decision.ACCEPT)),
        lambda port: services.cancel_ticket(port, placed.ticket_id, Trigger.TICKET_REFUSED),
    )

    assert (accepted, cancelled) == (State.CONFIRM_PENDING, False)
    assert (row(placed.ticket_id).state, bet_state(placed.bet_id)) == (State.CONFIRM_PENDING, BetState.ACCEPTED)
    assert (hold_state(placed.bet_id), balance()) == (HoldState.CAPTURED, FUNDS - STAKE)


async def test_decision_applied_through_run_db_commits_with_the_monolith_effect():
    run_db = _runner()
    try:
        placed = await run_db(funded_bet)

        applied = await run_db(services.apply_recommendation, PORT, placed.ticket_id, Decision.ACCEPT)
        assert isinstance(applied, services.Applied)
        assert applied.state == State.CONFIRM_PENDING

        assert await run_db(lambda: (bet_state(placed.bet_id), hold_state(placed.bet_id))) == (
            BetState.ACCEPTED,
            HoldState.CAPTURED,
        )
    finally:
        run_db.shutdown()


async def test_failed_run_db_unit_rolls_back_the_monolith_effect(monkeypatch):
    run_db = _runner()

    def fail(_):
        raise OperationalError("simulated")

    try:
        placed = await run_db(funded_bet)
        monkeypatch.setattr(tickets, "record_accept", fail)

        with pytest.raises(OperationalError):
            await run_db(services.apply_recommendation, PORT, placed.ticket_id, Decision.ACCEPT)

        assert await run_db(lambda: (bet_state(placed.bet_id), hold_state(placed.bet_id), balance())) == (
            BetState.PENDING_RISK,
            HoldState.HELD,
            FUNDS - STAKE,
        )
    finally:
        run_db.shutdown()


class _PausingPort:
    """Pauses the first unit after its monolith effect, inside its transaction, until the second unit waits."""

    def __init__(self, inside: threading.Event, proceed: threading.Event) -> None:
        self._inside = inside
        self._proceed = proceed

    def accept(self, bet_id: int) -> AcceptOutcome:
        outcome = PORT.accept(bet_id)
        self._pause()
        return outcome

    def decline(self, bet_id: int) -> None:
        PORT.decline(bet_id)
        self._pause()

    def void(self, bet_id: int) -> None:
        PORT.void(bet_id)
        self._pause()

    def _pause(self) -> None:
        self._inside.set()
        assert self._proceed.wait(WAIT)


def _race(first: Callable[[BetDecisionPort], Any], second: Callable[[BetDecisionPort], Any]) -> tuple[Any, Any]:
    """`first` holds its transaction open after its monolith effect; `second` starts and must wait for a lock
    (on the ticket, or on the bet if the ticket lock is missing); then `first` commits."""
    inside, proceed = threading.Event(), threading.Event()
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(_in_tx, lambda: first(_PausingPort(inside, proceed)))
        assert inside.wait(WAIT)
        b = pool.submit(_in_tx, lambda: second(PORT))
        try:
            _wait_for_a_lock_waiter()
        finally:
            proceed.set()
        return a.result(WAIT), b.result(WAIT)


def _in_tx(fn: Callable[[], Any]) -> Any:
    try:
        with transaction.atomic():
            return fn()
    finally:
        connection.close()


def _wait_for_a_lock_waiter() -> None:
    with connection.cursor() as cursor:
        for _ in range(int(WAIT / 0.01)):
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            (waiting,) = cursor.fetchone()
            if waiting:
                return
            time.sleep(0.01)
    raise AssertionError("the second unit of work never waited for a lock")


def _runner() -> DbRunner:
    return DbRunner(
        max_workers=2,
        statement_timeout=timedelta(seconds=1),
        lock_timeout=timedelta(seconds=1),
        idle_in_transaction_timeout=timedelta(seconds=WAIT),
        await_timeout=timedelta(seconds=WAIT),
        admission_timeout=timedelta(seconds=WAIT),
    )
