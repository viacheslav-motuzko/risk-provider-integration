"""Shutdown, probes and startup, end to end in-process: the real loops, a real Postgres, the fake provider over a
real socket. The drain is driven by `IntegrationApp.shutdown`, which the SIGTERM handler calls; the signal itself
is covered by `test_command_process.py`."""

import threading
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from datetime import timedelta
from typing import Any

import aiohttp
import pytest
from django.db.migrations.recorder import MigrationRecorder
from structlog.testing import capture_logs

from apps.risk_integration.domain.states import State
from apps.risk_integration.persistence.db import DbRunner
from apps.risk_integration.ports import AcceptOutcome
from apps.risk_integration.protocol.close_codes import CLOSE_GOING_AWAY
from apps.risk_integration.protocol.messages import OutboundType
from apps.risk_integration.tests.tickets_helpers import latest_migration, row, state
from tests.flow_helpers import PORT, funded_bet
from tests.scenario_helpers import TOKEN, Running, count, counted, make_db_runner, running
from tools.fake_provider.server import WAIT, FakeProvider, RejectNew

pytestmark = pytest.mark.django_db(transaction=True)

TICKET = OutboundType.TICKET
CONFIRM = OutboundType.CONFIRM_RECOMMENDATION
ENDED = "risk_sessions_ended_total"


@pytest.fixture
async def db() -> AsyncIterator[DbRunner]:
    runner = make_db_runner()
    yield runner
    runner.shutdown()


@pytest.fixture
async def fake() -> AsyncIterator[FakeProvider]:
    async with FakeProvider(token=TOKEN) as provider:
        yield provider


class GatedAccept:
    """The real adapter, except that `accept` waits for `go` inside its unit of work and counts who waits."""

    def __init__(self) -> None:
        self.go = threading.Event()
        self.waiting = 0
        self._lock = threading.Lock()

    def accept(self, bet_id: int) -> AcceptOutcome:
        with self._lock:
            self.waiting += 1
        assert self.go.wait(WAIT)
        return PORT.accept(bet_id)

    def decline(self, bet_id: int) -> None:
        PORT.decline(bet_id)

    def void(self, bet_id: int) -> None:
        PORT.void(bet_id)


def holds(predicate: Callable[[], bool]) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return predicate()

    return check


def logged(logs: list[MutableMapping[str, Any]], event: str, **fields: Any) -> Callable[[], Awaitable[bool]]:
    return holds(lambda: any(e["event"] == event and fields.items() <= e.items() for e in logs))


async def status(run: Running, path: str) -> int:
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=WAIT)) as http,
        http.get(run.probe_url(path)) as response,
    ):
        return response.status


async def test_a_shutdown_lets_the_appliers_finish_sends_their_decisions_once_and_closes_with_going_away(db, fake):
    gated = GatedAccept()
    decided = count("risk_time_to_decision_seconds_count", decision="ACCEPT")
    with capture_logs() as logs:
        async with running(fake, port=gated) as run:
            bets = [await db(funded_bet) for _ in range(3)]
            # Two appliers hold a recommendation each, the third waits in the queue.
            await run.until(holds(lambda: gated.waiting == 2 and count("risk_inbound_queue_depth") == 1))
            assert run.app is not None
            run.app.shutdown.request("test")
            await run.until(logged(logs, "drain_started"))
            # The reader and the relay have stopped: a bet placed now is left for the next session.
            late = await db(funded_bet)
            assert await status(run, "/readyz") == 503
            gated.go.set()
            await run.wait(run.task)

    assert await fake.wait_for_close() == [CLOSE_GOING_AWAY]
    for bet in bets:
        assert await db(state, bet.ticket_id) == State.CONFIRMED
        assert len(fake.frames(CONFIRM, bet.ticket_id)) == 1
    late_row = await db(row, late.ticket_id)
    assert (late_row.state, late_row.locked_by) == (State.PENDING_SEND, "")
    assert fake.frames(TICKET, late.ticket_id) == []
    assert count("risk_time_to_decision_seconds_count", decision="ACCEPT") == decided + 3


async def test_a_drain_that_runs_out_of_time_still_closes_and_a_successor_sends_what_was_left(db, fake):
    gated = GatedAccept()
    with capture_logs() as logs:
        async with running(fake, port=gated, drain_timeout=timedelta(milliseconds=100)) as run:
            bet = await db(funded_bet)
            await run.until(holds(lambda: gated.waiting == 1))
            assert run.app is not None
            run.app.shutdown.request("test")
            await run.until(logged(logs, "drain_timed_out"))
            # The apply outlives the drain and commits after the session is gone: decided, but not sent.
            gated.go.set()
            await run.wait(run.task)

    assert await fake.wait_for_close() == [CLOSE_GOING_AWAY]
    left = await db(row, bet.ticket_id)
    assert (left.state, left.locked_by, fake.frames(CONFIRM, bet.ticket_id)) == (State.CONFIRM_PENDING, "", [])
    async with running(fake) as successor:
        await successor.wait(fake.wait_for(CONFIRM, bet.ticket_id))
        await successor.until(lambda: _is(db, bet.ticket_id, State.CONFIRMED))


async def test_a_shutdown_without_a_session_exits_without_waiting_out_the_backoff(db, fake):
    fake.token = "rotated"  # every connect is refused, and the next attempt is a minute away
    refused = count(ENDED, outcome="auth_failed", inbound_queue_full="false")
    long = timedelta(seconds=60)
    async with running(fake, auth_backoff_initial=long, auth_backoff_max=long) as run:
        await run.until(counted(ENDED, refused + 1, outcome="auth_failed", inbound_queue_full="false"))
        assert run.app is not None
        run.app.shutdown.request("test")
        await run.wait(run.task)  # bounded by WAIT, far below the backoff
    assert fake.sessions_opened == 0


async def test_a_refused_standby_is_ready_and_live_because_it_can_take_over(db, fake):
    fake.reject_new = RejectNew.UPGRADE
    async with running(fake) as holder:
        await holder.wait(fake.wait_for_session())
        refused = count(ENDED, outcome="already_connected", inbound_queue_full="false")
        async with running(fake, instance_name="standby") as standby:
            await standby.until(counted(ENDED, refused + 1, outcome="already_connected", inbound_queue_full="false"))
            assert [await status(standby, p) for p in ("/startupz", "/livez", "/readyz")] == [200, 200, 200]
    assert fake.sessions_opened == 1


async def test_no_loop_runs_until_every_migration_is_applied(db, fake):
    await db(MigrationRecorder.Migration.objects.filter(app="risk_integration", name=latest_migration()).delete)
    try:
        with capture_logs() as logs:
            async with running(fake, startup_retry=timedelta(milliseconds=20)) as run:
                await run.until(logged(logs, "startup_waiting", reason="migrations_pending"))
                assert [await status(run, p) for p in ("/startupz", "/readyz")] == [503, 503]
                assert fake.sessions_opened == 0
                await db(_record_applied)
                await run.wait(fake.wait_for_session())
                assert await status(run, "/startupz") == 200
    finally:
        await db(_record_applied)


def _record_applied() -> None:
    MigrationRecorder.Migration.objects.get_or_create(app="risk_integration", name=latest_migration())


async def _is(db: DbRunner, ticket_id: Any, expected: State) -> bool:
    return await db(state, ticket_id) == expected
