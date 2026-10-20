import asyncio
import contextvars
import threading
from datetime import timedelta
from typing import Any

import pytest
from django.db import DataError, IntegrityError, OperationalError, ProgrammingError, connection, transaction

from apps.risk_integration.domain.errors import PermanentApplyError
from apps.risk_integration.domain.states import State
from apps.risk_integration.models import RiskTicket
from apps.risk_integration.persistence import tickets
from apps.risk_integration.persistence.db import DbRunner, OutcomeUnknown
from apps.risk_integration.persistence.errors import ApplyFailure, classify_apply_failure
from apps.risk_integration.tests.tickets_helpers import new_ticket, state

WAIT = 5  # seconds; only bounds a hang, never waited out in a passing run

request_id = contextvars.ContextVar[str]("request_id")


def runner(*, max_workers: int = 2, await_timeout: timedelta = timedelta(seconds=2)) -> DbRunner:
    return DbRunner(
        max_workers=max_workers,
        statement_timeout=timedelta(milliseconds=200),
        lock_timeout=timedelta(milliseconds=150),
        idle_in_transaction_timeout=timedelta(seconds=WAIT),
        await_timeout=await_timeout,
    )


def _sql(query: str) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(query)
        return cursor.fetchone()


@pytest.mark.django_db(transaction=True)
class TestRunDb:
    async def test_each_unit_of_work_is_one_transaction_with_local_timeouts(self):
        run_db = runner()

        def inspect():
            settings = _sql(
                "SELECT current_setting('statement_timeout'), current_setting('lock_timeout'), "
                "current_setting('idle_in_transaction_session_timeout')"
            )
            return connection.in_atomic_block, settings

        try:
            assert await run_db(inspect) == (True, ("200ms", "150ms", "5s"))
        finally:
            run_db.shutdown()

    async def test_timeouts_end_with_the_transaction(self):
        # SET LOCAL, not SET: behind a transaction pooler a session setting would leak to other clients.
        run_db = runner()

        def statement_timeout_after_commit():
            with connection.cursor() as cursor:
                cursor.execute("COMMIT")  # end the unit's transaction early, on the same connection
                cursor.execute("SHOW statement_timeout")
                return cursor.fetchone()[0]

        try:
            assert await run_db(statement_timeout_after_commit) == "0"
        finally:
            run_db.shutdown()

    async def test_statement_timeout_aborts_the_query_and_counts_as_transient(self):
        run_db = runner()
        try:
            with pytest.raises(OperationalError) as exc_info:
                await run_db(_sql, "SELECT pg_sleep(1)")
        finally:
            run_db.shutdown()
        assert classify_apply_failure(exc_info.value) is ApplyFailure.TRANSIENT

    async def test_context_variables_reach_the_worker_thread(self):
        run_db = runner()
        request_id.set("conn-42")
        try:
            assert await run_db(request_id.get) == "conn-42"
        finally:
            run_db.shutdown()

    async def test_failed_unit_of_work_rolls_back_everything_it_wrote(self):
        run_db = runner()
        tid = await run_db(new_ticket)

        def accept_then_fail():
            tickets.record_accept(tid)
            raise ValueError("boom")

        try:
            with pytest.raises(ValueError, match="boom"):
                await run_db(accept_then_fail)
            assert await run_db(state, tid) == State.PENDING_SEND
        finally:
            run_db.shutdown()

    async def test_timed_out_run_db_is_outcome_unknown_and_its_transaction_still_commits(self):
        run_db = runner(max_workers=1, await_timeout=timedelta(milliseconds=300))
        tid = await run_db(new_ticket)
        finish = threading.Event()

        def slow_accept():
            applied = tickets.record_accept(tid)
            assert finish.wait(WAIT)  # the caller gives up while this transaction is still open
            return applied

        try:
            with pytest.raises(OutcomeUnknown):
                await run_db(slow_accept)

            # The permit stays held until the worker thread finishes, so the next call waits for admission.
            # Had it been admitted, it would queue behind the busy thread and time out as well.
            pending_probe = asyncio.create_task(run_db(state, tid))
            await asyncio.sleep(0.6)
            assert not pending_probe.done()

            finish.set()
            assert await asyncio.wait_for(pending_probe, WAIT) == State.CONFIRM_PENDING
        finally:
            finish.set()
            run_db.shutdown()

    def test_await_timeout_must_exceed_statement_timeout(self):
        with pytest.raises(ValueError, match="await_timeout"):
            runner(await_timeout=timedelta(milliseconds=200))


@pytest.mark.django_db
class TestApplyFailureClassification:
    def test_integrity_and_data_errors_are_permanent(self):
        new_ticket()
        with pytest.raises(IntegrityError) as integrity, transaction.atomic():
            RiskTicket.objects.update(state="NOT_A_STATE")
        with pytest.raises(DataError) as data, transaction.atomic():
            _sql("SELECT 1/0")
        assert classify_apply_failure(integrity.value) is ApplyFailure.PERMANENT
        assert classify_apply_failure(data.value) is ApplyFailure.PERMANENT

    def test_broken_monolith_invariant_is_permanent(self):
        assert classify_apply_failure(PermanentApplyError("hold missing")) is ApplyFailure.PERMANENT

    def test_unknown_outcome_is_transient(self):
        assert classify_apply_failure(OutcomeUnknown("apply")) is ApplyFailure.TRANSIENT

    def test_bugs_stay_unclassified_so_the_process_fails_fast(self):
        with pytest.raises(ProgrammingError) as programming, transaction.atomic():
            _sql("SELECT * FROM no_such_table")
        assert classify_apply_failure(programming.value) is None
        assert classify_apply_failure(ValueError("bug")) is None
