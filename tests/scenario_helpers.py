"""Running the integration in-process against the fake provider, for scenario tests."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Any

from django.conf import settings
from prometheus_client import REGISTRY
from pydantic import SecretStr

from apps.risk_integration.persistence.db import DbRunner
from apps.risk_integration.ports import BetDecisionPort
from apps.risk_integration.runtime.app import IntegrationApp
from apps.risk_integration.runtime.config import IntegrationSettings
from apps.risk_integration.transport.ws_client import StubTokenProvider
from tests.flow_helpers import PORT
from tools.fake_provider.server import WAIT, FakeProvider

TOKEN = "scenario-token"


def integration_settings(url: str, **overrides: Any) -> IntegrationSettings:
    values: dict[str, Any] = {
        "provider_url": url,
        "provider_token": SecretStr(TOKEN),
        "provider_allow_plaintext": True,
        "instance_name": "scenario",
        "appliers": 2,
        "relay_poll_interval": timedelta(milliseconds=20),
        "sweeper_interval": timedelta(milliseconds=20),
        # Short enough that a scenario sees several reconnects within its wait.
        "reconnect_initial": timedelta(milliseconds=20),
        "reconnect_max": timedelta(milliseconds=100),
        "standby_retry": timedelta(milliseconds=50),
        "displaced_backoff": timedelta(milliseconds=100),
        "auth_backoff_initial": timedelta(milliseconds=50),
        "auth_backoff_max": timedelta(milliseconds=100),
        **overrides,
    }
    return IntegrationSettings(**values)


def make_db_runner() -> DbRunner:
    return DbRunner(
        max_workers=2,
        statement_timeout=timedelta(seconds=2),
        lock_timeout=timedelta(seconds=2),
        idle_in_transaction_timeout=timedelta(seconds=WAIT),
        await_timeout=timedelta(seconds=WAIT),
        admission_timeout=timedelta(seconds=WAIT),
    )


class Running:
    """The app's task. Every wait also watches it, so a crash fails the test with its own error, not a timeout."""

    def __init__(self, task: asyncio.Task[None]) -> None:
        self.task = task

    async def stop(self) -> None:
        """Cancels the app, which closes its session; as close to a pod going away as a test gets in-process."""
        if not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def wait[T](self, awaitable: Awaitable[T]) -> T:
        waiter = asyncio.ensure_future(awaitable)
        done, _ = await asyncio.wait({waiter, self.task}, timeout=WAIT, return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            return waiter.result()
        waiter.cancel()
        if self.task in done:
            self.task.result()
            raise AssertionError("the integration stopped")
        raise AssertionError("timed out")

    async def until(self, check: Callable[[], Awaitable[bool]]) -> None:
        async def poll() -> None:
            while not await check():  # noqa: ASYNC110 - polls the database; there is no event to wait on
                await asyncio.sleep(0.02)

        await self.wait(poll())


@asynccontextmanager
async def running(
    fake: FakeProvider, *, port: BetDecisionPort = PORT, token: str = TOKEN, **overrides: Any
) -> AsyncIterator[Running]:
    app = IntegrationApp(
        integration_settings(fake.url, **overrides),
        ticket_deadline=settings.RISK_TICKET_DEADLINE,
        port=port,
        tokens=StubTokenProvider(SecretStr(token)),
    )
    run = Running(asyncio.create_task(app.run()))
    try:
        yield run
    finally:
        await run.stop()


def count(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def counted(name: str, at_least: float, **labels: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return count(name, **labels) >= at_least

    return check
