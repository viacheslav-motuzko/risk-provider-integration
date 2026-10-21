import asyncio
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any, cast

import aiohttp
import pytest
from django.db import OperationalError, connection
from django.db.migrations.recorder import MigrationRecorder

from apps.risk_integration.adapters.monolith import DjangoBetDecisionAdapter
from apps.risk_integration.persistence.db import DbRunner
from apps.risk_integration.persistence.schema import pending_migrations
from apps.risk_integration.runtime.decisions import Decisions
from apps.risk_integration.runtime.health import Health, Heartbeat, Heartbeats, serve_health
from apps.risk_integration.runtime.relay import Relay, RelaySettings
from apps.risk_integration.runtime.shutdown import Shutdown
from apps.risk_integration.runtime.sweeper import Sweeper, SweeperSettings
from apps.risk_integration.tests.tickets_helpers import latest_migration

WAIT = 5
STALE_AFTER = timedelta(seconds=60)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestHeartbeats:
    def test_a_loop_that_beat_within_the_limit_is_on_time_and_one_that_did_not_is_stale(self):
        clock = Clock()
        heartbeats = Heartbeats(STALE_AFTER, clock=clock)
        heartbeats.loop("sweeper").beat()
        clock.advance(60)
        assert heartbeats.report() == {"sweeper": "ok"}
        clock.advance(1)
        assert heartbeats.report() == {"sweeper": "stale"}

    def test_a_planned_wait_moves_the_deadline_by_its_length(self):
        clock = Clock()
        heartbeats = Heartbeats(STALE_AFTER, clock=clock)
        heartbeats.loop("connection").beat(extra=timedelta(minutes=5))  # an auth backoff
        clock.advance(60 + 299)
        assert heartbeats.report() == {"connection": "ok"}
        clock.advance(2)
        assert heartbeats.report() == {"connection": "stale"}

    def test_a_parked_loop_is_never_stale_until_it_beats_again(self):
        clock = Clock()
        heartbeats = Heartbeats(STALE_AFTER, clock=clock)
        applier = heartbeats.loop("applier-0")
        applier.beat()
        applier.park()  # waits on an empty queue
        clock.advance(3600)
        assert heartbeats.report() == {"applier-0": "parked"}
        applier.beat()
        clock.advance(61)
        assert heartbeats.report() == {"applier-0": "stale"}


class _DbDown:
    """A runner whose every unit of work fails as if the database were gone."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise OperationalError("connection refused")


def _sweeper(run_db: DbRunner, heartbeat: Heartbeat) -> Coroutine[Any, Any, Any]:
    settings = SweeperSettings(
        interval=timedelta(milliseconds=10), batch_size=10, retry_failed_after=timedelta(seconds=1)
    )
    return Sweeper(run_db, Decisions(run_db, DjangoBetDecisionAdapter()), settings, heartbeat).run()


def _relay(run_db: DbRunner, heartbeat: Heartbeat) -> Coroutine[Any, Any, Any]:
    settings = RelaySettings(
        owner="relay-a",
        lease=timedelta(seconds=10),
        batch_size=10,
        poll_interval=timedelta(milliseconds=10),
        retransmit_after=timedelta(seconds=5),
        loss_window=timedelta(seconds=40),
    )
    return Relay(run_db, settings, heartbeat).run(_NeverSends())


class _NeverSends:
    async def send(self, frame: str) -> None:
        raise AssertionError("nothing to send while the database is down")


@pytest.mark.parametrize("make_loop", [_sweeper, _relay], ids=["sweeper", "relay"])
async def test_a_loop_keeps_beating_while_the_database_is_down_so_liveness_stays_green(make_loop):
    heartbeats = Heartbeats(timedelta(milliseconds=100))
    db_down = _DbDown()
    task = asyncio.create_task(make_loop(cast(DbRunner, db_down), heartbeats.loop("loop")))
    try:
        # Several times the stale limit, with the database failing on every pass.
        for _ in range(20):
            await asyncio.sleep(0.02)
            assert heartbeats.report() == {"loop": "ok"}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert db_down.calls > 5


class TestReadiness:
    def make(self, clock: Clock, last_db_success: float | None = None) -> tuple[Health, Shutdown]:
        shutdown = Shutdown()
        health = Health(
            Heartbeats(STALE_AFTER, clock=clock),
            shutdown,
            last_db_success=lambda: last_db_success,
            ready_db_within=timedelta(seconds=10),
            clock=clock,
        )
        return health, shutdown

    def test_a_started_pod_that_reached_the_database_lately_is_ready_with_or_without_a_session(self):
        clock = Clock()
        health, _ = self.make(clock, last_db_success=clock.now - 10)
        assert not health.readiness()[0]
        health.mark_started()
        assert health.readiness() == (True, {"started": True, "draining": False, "db_reachable": True})

    def test_a_pod_that_has_not_reached_the_database_lately_is_not_ready(self):
        clock = Clock()
        health, _ = self.make(clock, last_db_success=clock.now - 11)
        health.mark_started()
        assert health.readiness()[1]["db_reachable"] is False
        assert not health.readiness()[0]

    def test_a_draining_pod_is_not_ready_because_it_can_no_longer_take_over(self):
        clock = Clock()
        health, shutdown = self.make(clock, last_db_success=clock.now)
        health.mark_started()
        shutdown.request("SIGTERM")
        assert not health.readiness()[0]


async def test_the_probes_answer_over_http_with_their_status_and_the_metrics_are_served():
    clock = Clock()
    heartbeats = Heartbeats(STALE_AFTER, clock=clock)
    health = Health(
        heartbeats, Shutdown(), last_db_success=lambda: clock.now, ready_db_within=timedelta(seconds=10), clock=clock
    )
    heartbeats.loop("sweeper").beat()

    async with serve_health(health, host="127.0.0.1", port=0) as port, aiohttp.ClientSession() as http:

        async def status(path: str) -> int:
            async with http.get(f"http://127.0.0.1:{port}{path}", timeout=aiohttp.ClientTimeout(total=WAIT)) as r:
                return r.status

        assert (await status("/startupz"), await status("/readyz"), await status("/livez")) == (503, 503, 200)
        health.mark_started()
        assert (await status("/startupz"), await status("/readyz")) == (200, 200)
        clock.advance(61)
        assert await status("/livez") == 503
        async with http.get(f"http://127.0.0.1:{port}/metrics") as metrics:
            assert "risk_session_open" in await metrics.text()


@pytest.mark.django_db
def test_a_migration_not_yet_applied_is_reported_pending():
    assert pending_migrations() == []
    latest = latest_migration()
    MigrationRecorder(connection).record_unapplied("risk_integration", latest)
    assert pending_migrations() == [f"risk_integration.{latest}"]
