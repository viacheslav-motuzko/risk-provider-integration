import asyncio
from datetime import timedelta
from enum import StrEnum
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.core.management import CommandError, call_command
from pydantic import SecretStr, ValidationError
from structlog.testing import capture_logs

from apps.risk_integration.domain import states
from apps.risk_integration.domain.states import State
from apps.risk_integration.management.commands import run_risk_provider
from apps.risk_integration.persistence.db import DbRunner
from apps.risk_integration.protocol import messages
from apps.risk_integration.protocol.close_codes import SessionOutcome
from apps.risk_integration.runtime.config import IntegrationSettings
from apps.risk_integration.runtime.relay import Relay, RelaySettings
from apps.risk_integration.runtime.supervisor import LoopReturned, supervise
from apps.risk_integration.tests.tickets_helpers import new_ticket, row, state
from apps.risk_integration.transport.ws_client import SessionEnded

WAIT = 5


def settings(**overrides: Any) -> IntegrationSettings:
    values: dict[str, Any] = {
        "provider_url": "wss://provider.example",
        "provider_token": SecretStr("s3cr3t"),
        **overrides,
    }
    return IntegrationSettings(**values)


class TestSupervisor:
    async def test_a_loop_that_returns_is_a_failure_and_cancels_the_others(self):
        cancelled = asyncio.Event()

        async def forever():
            try:
                await asyncio.sleep(WAIT)
            finally:
                cancelled.set()

        async def returns():
            return None

        with pytest.raises(ExceptionGroup) as failure:
            await asyncio.wait_for(supervise({"forever": forever, "returns": returns}), WAIT)

        assert failure.group_contains(LoopReturned, match="returns")
        assert cancelled.is_set()

    async def test_a_loop_that_fails_ends_the_group_with_its_error(self):
        async def fails():
            raise ValueError("bug")

        with pytest.raises(ExceptionGroup) as failure:
            await asyncio.wait_for(supervise({"fails": fails, "forever": lambda: asyncio.sleep(WAIT)}), WAIT)

        assert failure.group_contains(ValueError, match="bug")


@pytest.mark.parametrize(
    ("persisted", "wire"),
    [
        (states.CancelReason, messages.CancellationReason),
        (states.DeclineReason, messages.DeclineReason),
        (states.Decision, messages.Decision),
        (states.MessageRef, messages.OutboundType),
    ],
)
def test_persisted_and_wire_enums_map_one_to_one_by_value(persisted: type[StrEnum], wire: type[StrEnum]) -> None:
    # The domain cannot import the protocol; the runtime maps between them by value.
    assert {m.value for m in persisted} == {m.value for m in wire}


class _DeadSession:
    async def send(self, frame: str) -> None:
        raise SessionEnded(SessionOutcome.TRANSIENT, "connection lost")


@pytest.mark.django_db(transaction=True)
async def test_a_send_that_fails_is_not_recorded_so_the_message_is_still_owed():
    run_db = DbRunner(
        max_workers=2,
        statement_timeout=timedelta(seconds=1),
        lock_timeout=timedelta(seconds=1),
        idle_in_transaction_timeout=timedelta(seconds=WAIT),
        await_timeout=timedelta(seconds=WAIT),
        admission_timeout=timedelta(seconds=WAIT),
    )
    relay = Relay(
        run_db,
        RelaySettings(
            owner="relay-a",
            lease=timedelta(seconds=10),
            batch_size=10,
            poll_interval=timedelta(milliseconds=10),
            retransmit_after=timedelta(seconds=5),
            loss_window=timedelta(seconds=40),
        ),
    )
    try:
        tid = await run_db(new_ticket)
        with pytest.raises(SessionEnded):
            await asyncio.wait_for(relay.run(_DeadSession()), WAIT)
        ticket = await run_db(row, tid)
        assert await run_db(state, tid) == State.PENDING_SEND
        assert ticket.last_sent_at is None
    finally:
        run_db.shutdown()


class TestSettings:
    def test_durations_come_from_the_environment_in_seconds(self, monkeypatch):
        monkeypatch.setenv("RISK_RELAY_POLL_INTERVAL", "0.25")
        assert settings().relay_poll_interval == timedelta(milliseconds=250)

    def test_plain_ws_needs_an_explicit_opt_in(self):
        with pytest.raises(ValidationError, match="wss://"):
            settings(provider_url="ws://provider.example")
        assert settings(provider_url="ws://localhost:8765", provider_allow_plaintext=True)

    def test_the_lease_must_outlast_a_send_and_its_commit(self):
        with pytest.raises(ValidationError, match="RISK_LEASE"):
            settings(lease=timedelta(seconds=5))

    def test_the_token_never_shows_in_a_repr(self):
        assert "s3cr3t" not in repr(settings())
        assert "s3cr3t" not in str(settings())

    def test_the_loss_window_is_how_long_a_dead_connection_looks_alive(self):
        assert settings().loss_window == timedelta(seconds=40)


def test_the_command_refuses_invalid_settings_without_echoing_values(monkeypatch):
    monkeypatch.delenv("RISK_PROVIDER_URL", raising=False)
    monkeypatch.setenv("RISK_PROVIDER_TOKEN", "s3cr3t")
    monkeypatch.setenv("RISK_APPLIERS", "not-a-number")

    with pytest.raises(CommandError) as failure:
        call_command("run_risk_provider")

    message = str(failure.value)
    assert "RISK_PROVIDER_URL" in message
    assert "RISK_APPLIERS" in message
    assert "s3cr3t" not in message
    assert "not-a-number" not in message


class _IdleApp:
    async def run(self) -> None:
        return None


@pytest.mark.parametrize(("conn_max_age", "warned"), [(0, True), (60, False)])
def test_the_command_warns_when_db_connections_are_not_persistent(monkeypatch, conn_max_age, warned):
    monkeypatch.setenv("RISK_PROVIDER_URL", "wss://provider.example")
    monkeypatch.setenv("RISK_PROVIDER_TOKEN", "s3cr3t")
    monkeypatch.setitem(django_settings.DATABASES["default"], "CONN_MAX_AGE", conn_max_age)
    monkeypatch.setattr(run_risk_provider, "build_app", lambda *_, **__: _IdleApp())

    with capture_logs() as logs:
        call_command("run_risk_provider")

    assert any(e["event"] == "db_connections_not_persistent" for e in logs) is warned
