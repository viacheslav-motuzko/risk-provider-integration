"""Real threads, each with its own connection and transaction, against the real database."""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, transaction

from apps.risk_integration.persistence import tickets
from apps.risk_integration.tests.tickets_helpers import LEASE, new_ticket

pytestmark = pytest.mark.django_db(transaction=True)

WAIT = 5  # seconds; only bounds a hang, never waited out in a passing run


def in_tx[T](fn: Callable[[], T]) -> T:
    try:
        with transaction.atomic():
            return fn()
    finally:
        connection.close()


def test_create_ticket_refuses_to_run_outside_the_bets_transaction():
    with pytest.raises(RuntimeError, match="inside the transaction"):
        new_ticket()


def test_concurrent_claimers_get_disjoint_rows_without_waiting_for_each_other():
    ids = {in_tx(new_ticket) for _ in range(6)}
    a_claimed = threading.Event()
    b_done = threading.Event()

    def claimer_a():
        def claim():
            claims = tickets.claim_due(owner="a", lease=LEASE, limit=3)
            a_claimed.set()
            # Keep the row locks until B has claimed: B must skip them, not wait for them.
            assert b_done.wait(WAIT)
            return claims

        return in_tx(claim)

    def claimer_b():
        assert a_claimed.wait(WAIT)
        try:
            return in_tx(lambda: tickets.claim_due(owner="b", lease=LEASE, limit=10))
        finally:
            b_done.set()

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(claimer_a), pool.submit(claimer_b)
        a_ids = {c.ticket_id for c in a.result(WAIT)}
        b_ids = {c.ticket_id for c in b.result(WAIT)}

    assert len(a_ids) == 3
    assert a_ids.isdisjoint(b_ids)
    assert a_ids | b_ids == ids
    assert in_tx(lambda: tickets.claim_due(owner="c", lease=LEASE, limit=10)) == []


def test_duplicated_recommendation_applied_by_two_appliers_at_once_applies_exactly_once():
    ids = [in_tx(new_ticket) for _ in range(20)]
    barrier = threading.Barrier(2)

    def applier(tid):
        barrier.wait(WAIT)
        return in_tx(lambda: tickets.record_accept(tid))

    with ThreadPoolExecutor(2) as pool:
        for tid in ids:
            outcomes = [f.result(WAIT) for f in [pool.submit(applier, tid), pool.submit(applier, tid)]]
            assert sorted(outcomes) == [False, True]
