"""The command as its own process: the one place where the signal handler, the JSON logs and the probes over
HTTP can be seen together. One test, not an end-to-end suite: the behaviour behind them is tested in-process."""

import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from django.conf import settings

from apps.risk_integration.protocol.close_codes import CLOSE_GOING_AWAY
from tests.scenario_helpers import TOKEN
from tools.fake_provider.server import WAIT, FakeProvider

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parent.parent
STARTUP_WAIT = 30  # seconds; Django and the app start in a fresh interpreter


@pytest.fixture
async def fake() -> AsyncIterator[FakeProvider]:
    async with FakeProvider(token=TOKEN) as provider:
        yield provider


async def test_sigterm_drains_closes_with_going_away_and_exits_zero_with_json_logs(fake):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "manage.py",
        "run_risk_provider",
        cwd=ROOT,
        env=_environment(fake.url),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    lines: list[dict[str, Any]] = []
    try:
        port = (await _until_logged(process, lines, "health_serving"))["port"]
        await _until_logged(process, lines, "session_opened")
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=WAIT)) as http,
            http.get(f"http://127.0.0.1:{port}/readyz") as ready,
        ):
            assert ready.status == 200

        process.send_signal(signal.SIGTERM)
        async with asyncio.timeout(STARTUP_WAIT):
            rest, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    lines += [json.loads(line) for line in rest.decode().splitlines() if line]
    assert process.returncode == 0
    assert await fake.wait_for_close() == [CLOSE_GOING_AWAY]
    events = [line["event"] for line in lines]
    assert events.index("shutdown_requested") < events.index("drain_finished") < events.index("integration_stopped")
    opened = next(line for line in lines if line["event"] == "session_opened")
    assert opened["connection_id"]
    assert opened["timestamp"].endswith("Z")


def _environment(provider_url: str) -> dict[str, str]:
    db = settings.DATABASES["default"]
    env = {name: value for name, value in os.environ.items() if not name.startswith(("RISK_", "DJANGO_"))}
    return env | {
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "DJANGO_SECRET_KEY": "test-only",
        # The test database, which pytest-django created and migrated.
        "DATABASE_URL": f"postgres://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}",
        "RISK_PROVIDER_URL": provider_url,
        "RISK_PROVIDER_ALLOW_PLAINTEXT": "1",
        "RISK_PROVIDER_TOKEN": TOKEN,
        "RISK_HEALTH_HOST": "127.0.0.1",
        "RISK_HEALTH_PORT": "0",
    }


async def _until_logged(process: asyncio.subprocess.Process, lines: list[dict[str, Any]], event: str) -> dict[str, Any]:
    """Reads JSON lines until `event`; every line must be JSON."""
    assert process.stdout is not None
    async with asyncio.timeout(STARTUP_WAIT):
        while True:
            raw = await process.stdout.readline()
            assert raw, f"the process exited before logging {event}"
            line: dict[str, Any] = json.loads(raw)
            lines.append(line)
            if line["event"] == event:
                return line
