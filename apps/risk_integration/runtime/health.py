"""Probes and metrics over HTTP, on a port of their own.

- Startup: the database is reachable and every migration is applied. No loop runs before that.
- Liveness: every loop's heartbeat is on time. Never the provider or the database: an outage of either must not
  restart the pod, while a hung loop must.
- Readiness: this pod can take over the work: started, not draining, and a unit of work committed recently.
  Holding the provider session is not required, or a new pod would never become ready while the old one holds
  it and a rolling update would stall.
"""

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from enum import StrEnum

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from apps.risk_integration.runtime.shutdown import Shutdown

type Clock = Callable[[], float]


class LoopState(StrEnum):
    OK = "ok"
    PARKED = "parked"
    STALE = "stale"


class Heartbeats:
    """Each loop says when it will beat next; liveness fails once any of those deadlines has passed.

    `beat()` promises the next beat within `stale_after` plus the loop's own planned wait. `park()` means the loop
    waits on something that may legitimately take forever (an empty queue, an open session watched by its own
    loops), so it has no deadline until it beats again. A loop starts parked.
    """

    def __init__(self, stale_after: timedelta, *, clock: Clock = time.monotonic) -> None:
        self._stale_after = stale_after.total_seconds()
        self._clock = clock
        self._deadlines: dict[str, float | None] = {}

    def loop(self, name: str) -> "Heartbeat":
        self._deadlines[name] = None
        return Heartbeat(self, name)

    def report(self) -> dict[str, LoopState]:
        now = self._clock()
        return {name: self._loop_state(deadline, now) for name, deadline in sorted(self._deadlines.items())}

    def beat(self, name: str, extra: timedelta) -> None:
        self._deadlines[name] = self._clock() + self._stale_after + extra.total_seconds()

    def park(self, name: str) -> None:
        self._deadlines[name] = None

    @staticmethod
    def _loop_state(deadline: float | None, now: float) -> LoopState:
        if deadline is None:
            return LoopState.PARKED
        return LoopState.STALE if deadline < now else LoopState.OK


class Heartbeat:
    """One loop's handle on `Heartbeats`."""

    def __init__(self, heartbeats: Heartbeats, name: str) -> None:
        self._heartbeats = heartbeats
        self._name = name

    def beat(self, *, extra: timedelta = timedelta(0)) -> None:
        """Call on every iteration, also when its database call failed; `extra` covers a planned wait."""
        self._heartbeats.beat(self._name, extra)

    def park(self) -> None:
        self._heartbeats.park(self._name)


class Health:
    def __init__(
        self,
        heartbeats: Heartbeats,
        shutdown: Shutdown,
        *,
        last_db_success: Callable[[], float | None],
        ready_db_within: timedelta,
        clock: Clock = time.monotonic,
    ) -> None:
        self._heartbeats = heartbeats
        self._shutdown = shutdown
        self._last_db_success = last_db_success
        self._ready_db_within = ready_db_within.total_seconds()
        self._clock = clock
        self._started = False

    def mark_started(self) -> None:
        self._started = True

    def startup(self) -> tuple[bool, dict[str, object]]:
        return self._started, {"started": self._started}

    def liveness(self) -> tuple[bool, dict[str, object]]:
        loops = self._heartbeats.report()
        return LoopState.STALE not in loops.values(), {"loops": loops}

    def readiness(self) -> tuple[bool, dict[str, object]]:
        last = self._last_db_success()
        db_reachable = last is not None and self._clock() - last <= self._ready_db_within
        draining = self._shutdown.requested
        detail: dict[str, object] = {"started": self._started, "draining": draining, "db_reachable": db_reachable}
        return self._started and not draining and db_reachable, detail


@asynccontextmanager
async def serve_health(health: Health, *, host: str, port: int) -> AsyncIterator[int]:
    """Serves the probes and `/metrics` while the context is open; yields the bound port."""
    app = web.Application()
    app.router.add_get("/startupz", _probe(health.startup))
    app.router.add_get("/livez", _probe(health.liveness))
    app.router.add_get("/readyz", _probe(health.readiness))
    app.router.add_get("/metrics", _metrics)

    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port)
        await site.start()
        yield runner.addresses[0][1]
    finally:
        await runner.cleanup()


def _probe(check: Callable[[], tuple[bool, dict[str, object]]]) -> Callable[[web.Request], Awaitable[web.Response]]:
    async def handler(_: web.Request) -> web.Response:
        ok, detail = check()
        return web.Response(status=200 if ok else 503, text=json.dumps(detail), content_type="application/json")

    return handler


async def _metrics(_: web.Request) -> web.Response:
    return web.Response(body=generate_latest(REGISTRY), headers={"Content-Type": CONTENT_TYPE_LATEST})
