import asyncio
import json
import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from enum import StrEnum
from typing import Any, cast

import pytest
from django.conf import settings as django_settings
from django.core.management import CommandError, call_command
from django.db import transaction
from pydantic import SecretStr, ValidationError
from structlog.testing import capture_logs

from apps.risk_integration.domain import states
from apps.risk_integration.domain.machine import Trigger
from apps.risk_integration.domain.states import State
from apps.risk_integration.management.commands import run_risk_provider
from apps.risk_integration.persistence import tickets
from apps.risk_integration.persistence.db import DbRunner
from apps.risk_integration.protocol import messages
from apps.risk_integration.protocol.close_codes import SessionOutcome
from apps.risk_integration.runtime.config import IntegrationSettings
from apps.risk_integration.runtime.health import Heartbeat, Heartbeats
from apps.risk_integration.runtime.relay import Relay, RelaySettings
from apps.risk_integration.runtime.shutdown import Shutdown
from apps.risk_integration.runtime.supervisor import LoopReturned, supervise
from apps.risk_integration.tests.tickets_helpers import new_ticket, row, state
from apps.risk_integration.transport.ws_client import SessionEnded, fetch_token

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


class _SessionThatDies:
    """Takes `frames` frames, then the connection is gone."""

    def __init__(self, *, frames: int) -> None:
        self.frames = frames

    async def send(self, frame: str) -> None:
        if self.frames == 0:
            raise SessionEnded(SessionOutcome.TRANSIENT, "connection lost")
        self.frames -= 1


class _StuckSession:
    """A write that never completes."""

    def __init__(self) -> None:
        self.writing = asyncio.Event()

    async def send(self, frame: str) -> None:
        self.writing.set()
        await asyncio.Event().wait()


class _ClaimHeldBack:
    """The real runner, except that `claim_due` waits for `go` inside its unit of work: a claim still running when
    the relay is cancelled, which commits afterwards."""

    def __init__(self, run_db: DbRunner) -> None:
        self.run_db = run_db
        self.started = asyncio.Event()
        self.go = threading.Event()
        self.committed = threading.Event()

    async def __call__(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        if getattr(fn, "func", None) is not tickets.claim_due:
            return await self.run_db(fn, *args, **kwargs)

        def held_back() -> Any:
            self.go.wait(WAIT)
            transaction.on_commit(self.committed.set)
            return fn(*args, **kwargs)

        self.started.set()
        return await self.run_db(held_back)


@pytest.fixture
def run_db() -> Iterator[DbRunner]:
    runner = DbRunner(
        max_workers=2,
        statement_timeout=timedelta(seconds=1),
        lock_timeout=timedelta(seconds=1),
        idle_in_transaction_timeout=timedelta(seconds=WAIT),
        await_timeout=timedelta(seconds=WAIT),
        admission_timeout=timedelta(seconds=WAIT),
    )
    yield runner
    runner.shutdown()


def make_relay(run_db: DbRunner, heartbeat: Heartbeat | None = None) -> Relay:
    return Relay(
        run_db,
        RelaySettings(
            owner="relay-a",
            lease=timedelta(seconds=10),
            batch_size=10,
            poll_interval=timedelta(milliseconds=10),
            retransmit_after=timedelta(seconds=5),
            loss_window=timedelta(seconds=40),
        ),
        heartbeat or Heartbeats(timedelta(seconds=60)).loop("relay"),
    )


@pytest.mark.django_db(transaction=True)
class TestRelay:
    async def test_a_send_that_fails_is_not_recorded_so_the_message_is_still_owed(self, run_db):
        relay = make_relay(run_db)
        tid = await run_db(new_ticket)
        with pytest.raises(SessionEnded):
            await asyncio.wait_for(relay.run(_SessionThatDies(frames=0)), WAIT)
        ticket = await run_db(row, tid)
        assert await run_db(state, tid) == State.PENDING_SEND
        assert ticket.last_sent_at is None

    async def test_a_session_that_dies_mid_batch_leaves_no_lease_on_the_rest_of_it(self, run_db):
        relay = make_relay(run_db)
        ids = [await run_db(new_ticket) for _ in range(3)]
        with pytest.raises(SessionEnded):
            await asyncio.wait_for(relay.run(_SessionThatDies(frames=1)), WAIT)
        rows = [await run_db(row, tid) for tid in ids]
        assert sorted(r.state for r in rows) == [State.PENDING_SEND, State.PENDING_SEND, State.SENT]
        assert [(r.locked_by, r.locked_until) for r in rows] == [("", None)] * 3

    async def test_a_relay_cancelled_mid_send_leaves_no_lease_on_its_batch(self, run_db):
        relay = make_relay(run_db)
        ids = [await run_db(new_ticket) for _ in range(2)]
        session = _StuckSession()
        task = asyncio.create_task(relay.run(session))
        await asyncio.wait_for(session.writing.wait(), WAIT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT)
        rows = [await run_db(row, tid) for tid in ids]
        assert [(r.state, r.locked_by) for r in rows] == [(State.PENDING_SEND, "")] * 2

    async def test_a_claim_that_commits_after_the_relay_is_cancelled_is_released_too(self, run_db):
        ids = [await run_db(new_ticket) for _ in range(2)]
        held_back = _ClaimHeldBack(run_db)
        task = asyncio.create_task(make_relay(cast(DbRunner, held_back)).run(_StuckSession()))
        await asyncio.wait_for(held_back.started.wait(), WAIT)
        task.cancel()
        held_back.go.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT)
        await asyncio.to_thread(held_back.committed.wait, WAIT)

        rows = [await run_db(row, tid) for tid in ids]
        assert [(r.state, r.locked_by) for r in rows] == [(State.PENDING_SEND, "")] * 2


class _RecordingSession:
    def __init__(self) -> None:
        self.types: list[str] = []

    async def send(self, frame: str) -> None:
        self.types.append(json.loads(frame)["type"])


@pytest.mark.django_db(transaction=True)
class TestFlush:
    async def test_a_flush_sends_every_final_message_owed_and_no_ticket(self, run_db):
        pending, confirm, cancel = [await run_db(new_ticket) for _ in range(3)]
        await run_db(tickets.record_accept, confirm)
        await run_db(tickets.cancel, cancel, Trigger.TICKET_REFUSED, error_code="BAD")
        session = _RecordingSession()

        flushed = await asyncio.wait_for(make_relay(run_db).flush(session), WAIT)

        assert (flushed, sorted(session.types)) == (2, ["CONFIRM_RECOMMENDATION", "REQUEST_CANCELLATION"])
        assert (await run_db(state, confirm), await run_db(state, cancel)) == (State.CONFIRMED, State.CANCELLED)
        pending_row = await run_db(row, pending)
        assert (pending_row.state, pending_row.locked_by) == (State.PENDING_SEND, "")

    async def test_a_flush_pages_through_more_than_one_batch(self, run_db):
        ids = [await run_db(new_ticket) for _ in range(12)]  # the test relay's batch is 10
        for tid in ids:
            await run_db(tickets.record_decline, tid)

        assert await asyncio.wait_for(make_relay(run_db).flush(_RecordingSession()), WAIT) == 12

    async def test_a_flush_whose_session_dies_leaves_no_lease_on_the_rest_of_its_batch(self, run_db):
        ids = [await run_db(new_ticket) for _ in range(3)]
        for tid in ids:
            await run_db(tickets.record_accept, tid)

        with pytest.raises(SessionEnded):
            await asyncio.wait_for(make_relay(run_db).flush(_SessionThatDies(frames=1)), WAIT)

        rows = [await run_db(row, tid) for tid in ids]
        assert sorted(r.state for r in rows) == [State.CONFIRMED, State.CONFIRM_PENDING, State.CONFIRM_PENDING]
        assert [r.locked_by for r in rows] == [""] * 3


class _HangingTokens:
    async def token(self) -> SecretStr:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_a_token_fetch_that_hangs_ends_the_attempt_as_transient():
    with pytest.raises(SessionEnded) as ended:
        await asyncio.wait_for(fetch_token(_HangingTokens(), within=timedelta(milliseconds=20)), WAIT)
    assert ended.value.outcome is SessionOutcome.TRANSIENT


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

    @pytest.mark.parametrize(
        ("initial", "cap"), [("reconnect_initial", "reconnect_max"), ("auth_backoff_initial", "auth_backoff_max")]
    )
    def test_a_growing_reconnect_delay_cannot_start_above_its_cap(self, initial, cap):
        with pytest.raises(ValidationError, match=cap.upper()):
            settings(**{initial: timedelta(seconds=10), cap: timedelta(seconds=5)})

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


def test_the_command_refuses_a_reconnect_delay_that_outlasts_the_ticket_deadline(monkeypatch):
    monkeypatch.setenv("RISK_PROVIDER_URL", "wss://provider.example")
    monkeypatch.setenv("RISK_PROVIDER_TOKEN", "s3cr3t")
    monkeypatch.setenv("RISK_RECONNECT_MAX", str(django_settings.RISK_TICKET_DEADLINE.total_seconds()))

    with pytest.raises(CommandError, match="RISK_RECONNECT_MAX"):
        call_command("run_risk_provider")


class _IdleApp:
    def __init__(self) -> None:
        self.shutdown = Shutdown()

    async def run(self) -> None:
        return None


@pytest.mark.parametrize(("conn_max_age", "warned"), [(0, True), (60, False)])
def test_the_command_warns_when_db_connections_are_not_persistent(monkeypatch, conn_max_age, warned):
    monkeypatch.setenv("RISK_PROVIDER_URL", "wss://provider.example")
    monkeypatch.setenv("RISK_PROVIDER_TOKEN", "s3cr3t")
    monkeypatch.setitem(django_settings.DATABASES["default"], "CONN_MAX_AGE", conn_max_age)
    monkeypatch.setattr(run_risk_provider, "build_app", lambda *_, **__: _IdleApp())
    monkeypatch.setattr(run_risk_provider, "configure_logging", lambda _: None)  # global; tested on its own

    with capture_logs() as logs:
        call_command("run_risk_provider")

    assert any(e["event"] == "db_connections_not_persistent" for e in logs) is warned
