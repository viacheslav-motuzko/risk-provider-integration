"""The integration end to end, in-process: the real loops, a real Postgres, the fake provider over a real socket.
Time moves by writing `deadline_at` or `retransmit_at` into the past; nothing waits for a real deadline."""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from django.db import OperationalError

from apps.betting.models import BetState
from apps.risk_integration import services
from apps.risk_integration.domain.errors import PermanentApplyError
from apps.risk_integration.domain.machine import Trigger
from apps.risk_integration.domain.states import AWAITING_RECOMMENDATION, CancelReason, Decision, EscalationReason, State
from apps.risk_integration.persistence import tickets
from apps.risk_integration.persistence.db import DbRunner
from apps.risk_integration.ports import AcceptOutcome
from apps.risk_integration.protocol.close_codes import CLOSE_PLANNED
from apps.risk_integration.protocol.messages import Decision as WireDecision
from apps.risk_integration.protocol.messages import OutboundType
from apps.risk_integration.runtime.decisions import Decisions
from apps.risk_integration.runtime.sweeper import Sweeper, SweeperSettings
from apps.risk_integration.tests.tickets_helpers import ago, force, row, state
from apps.wallets.models import HoldState
from tests.flow_helpers import FUNDS, ODDS_OVER_LIMIT, PORT, STAKE, balance, bet_state, funded_bet, hold_state
from tests.scenario_helpers import TOKEN, Running, count, counted, make_db_runner, running
from tools.fake_provider.server import WAIT, FakeProvider, RejectNew, TicketReply

pytestmark = pytest.mark.django_db(transaction=True)

TICKET = OutboundType.TICKET
CONFIRM = OutboundType.CONFIRM_RECOMMENDATION
DECLINE = OutboundType.DECLINE_RECOMMENDATION
CANCEL = OutboundType.REQUEST_CANCELLATION
ENDED = "risk_sessions_ended_total"
CLOSE_SERVER_ERROR = 1011  # a standard code: transient, reconnect with backoff


@pytest.fixture
async def db() -> AsyncIterator[DbRunner]:
    runner = make_db_runner()
    yield runner
    runner.shutdown()


@pytest.fixture
async def fake() -> AsyncIterator[FakeProvider]:
    async with FakeProvider(token=TOKEN) as provider:
        yield provider


class FailingAccept:
    """The real adapter, except that `accept` raises `error` on its first `times` calls."""

    def __init__(self, error: Exception, *, times: int = 1_000) -> None:
        self.error = error
        self.times = times

    def accept(self, bet_id: int) -> AcceptOutcome:
        if self.times > 0:
            self.times -= 1
            raise self.error
        return PORT.accept(bet_id)

    def decline(self, bet_id: int) -> None:
        PORT.decline(bet_id)

    def void(self, bet_id: int) -> None:
        PORT.void(bet_id)


class FailingVoid:
    """The real adapter, except that `void` always raises `error` for one bet."""

    def __init__(self, bet_id: int, error: Exception) -> None:
        self.bet_id = bet_id
        self.error = error

    def accept(self, bet_id: int) -> AcceptOutcome:
        return PORT.accept(bet_id)

    def decline(self, bet_id: int) -> None:
        PORT.decline(bet_id)

    def void(self, bet_id: int) -> None:
        if bet_id == self.bet_id:
            raise self.error
        PORT.void(bet_id)


class StuckAccept:
    """The real adapter, except that `accept` waits until `released`: an applier stuck on a slow monolith."""

    def __init__(self) -> None:
        self.released = threading.Event()

    def accept(self, bet_id: int) -> AcceptOutcome:
        self.released.wait(WAIT)
        return PORT.accept(bet_id)

    def decline(self, bet_id: int) -> None:
        PORT.decline(bet_id)

    def void(self, bet_id: int) -> None:
        PORT.void(bet_id)


class FlakyRunDb:
    """The real runner, except that its first `failures` calls of `fn` raise `OperationalError`."""

    def __init__(self, run_db: DbRunner, fn: Callable[..., Any], *, failures: int) -> None:
        self.run_db = run_db
        self.fn = fn
        self.failures = failures

    async def __call__(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        if fn is self.fn and self.failures > 0:
            self.failures -= 1
            raise OperationalError("connection lost")
        return await self.run_db(fn, *args, **kwargs)


async def reaches(app: Running, db: DbRunner, ticket_id: UUID, expected: State) -> None:
    async def check() -> bool:
        return await db(state, ticket_id) == expected

    await app.until(check)


async def money(db: DbRunner, bet_id: int) -> tuple[str, str | None, Decimal]:
    def read() -> tuple[str, str | None, Decimal]:
        return bet_state(bet_id), hold_state(bet_id), balance()

    return await db(read)


# Normal flow


async def test_an_accepted_ticket_is_confirmed_and_its_stake_captured_once(db, fake):
    async with running(fake) as app:
        bet = await db(funded_bet)
        sent = await app.wait(fake.wait_for(TICKET, bet.ticket_id))
        await app.wait(fake.wait_for(CONFIRM, bet.ticket_id))
        await reaches(app, db, bet.ticket_id, State.CONFIRMED)

    assert sent.payload == {
        "stake": "10.00",
        "currency": "EUR",
        "odds": "1.8500",
        "selection_id": "sel-1",
        "customer_ref": "c-1",
    }
    assert await money(db, bet.bet_id) == (BetState.ACCEPTED, HoldState.CAPTURED, FUNDS - STAKE)
    assert len(fake.frames(TICKET, bet.ticket_id)) == 1


async def test_a_declined_ticket_releases_the_stake_and_sends_the_decline(db, fake):
    fake.ticket_reply = TicketReply.DECLINE
    async with running(fake) as app:
        bet = await db(funded_bet)
        sent = await app.wait(fake.wait_for(DECLINE, bet.ticket_id))
        await reaches(app, db, bet.ticket_id, State.DECLINED)

    assert sent.payload == {}
    assert await money(db, bet.bet_id) == (BetState.REJECTED, HoldState.RELEASED, FUNDS)


async def test_a_bet_our_limits_reject_is_declined_to_the_provider_with_the_reason(db, fake):
    async with running(fake) as app:
        bet = await db(funded_bet, odds=ODDS_OVER_LIMIT)
        sent = await app.wait(fake.wait_for(DECLINE, bet.ticket_id))
        await reaches(app, db, bet.ticket_id, State.DECLINED)

    assert sent.payload == {"reason": "LIMIT_EXCEEDED"}
    assert fake.frames(CONFIRM, bet.ticket_id) == []
    assert await money(db, bet.bet_id) == (BetState.REJECTED, HoldState.RELEASED, FUNDS)


async def test_a_duplicated_recommendation_moves_money_once_and_is_counted(db, fake):
    fake.duplicates = 3
    ignored = ("risk_inbound_ignored_total", {"message": "RECOMMENDATION", "reason": "not_awaiting"})
    before = count(ignored[0], **ignored[1])
    async with running(fake) as app:
        bet = await db(funded_bet)
        await reaches(app, db, bet.ticket_id, State.CONFIRMED)
        await app.until(counted(ignored[0], before + 2, **ignored[1]))

    assert await money(db, bet.bet_id) == (BetState.ACCEPTED, HoldState.CAPTURED, FUNDS - STAKE)
    assert len(fake.frames(CONFIRM, bet.ticket_id)) == 1


# Restart and crash recovery


async def test_after_a_crash_between_apply_and_confirm_the_next_process_sends_the_confirm(db, fake):
    bet = await db(funded_bet)
    await db(services.apply_recommendation, PORT, bet.ticket_id, Decision.ACCEPT)
    # The dead process had claimed the confirm and its lease has run out.
    await db(force, bet.ticket_id, locked_by="dead-pod", locked_until=ago(1))

    async with running(fake) as app:
        await app.wait(fake.wait_for(CONFIRM, bet.ticket_id))
        await reaches(app, db, bet.ticket_id, State.CONFIRMED)

    assert fake.frames(TICKET, bet.ticket_id) == []
    assert await money(db, bet.bet_id) == (BetState.ACCEPTED, HoldState.CAPTURED, FUNDS - STAKE)


async def test_after_a_restart_sent_tickets_are_retransmitted_at_once_and_resolved(db, fake):
    bet = await db(funded_bet)
    # Sent by the previous process, whose session died before the recommendation came; the retransmit point
    # is still far away.
    await db(force, bet.ticket_id, state=State.SENT, last_sent_at=ago(3), retransmit_at=ago(-60))
    resent = count("risk_messages_sent_total", type="TICKET", attempt="resend")

    async with running(fake) as app:
        await app.wait(fake.wait_for(TICKET, bet.ticket_id))
        await reaches(app, db, bet.ticket_id, State.CONFIRMED)

    assert len(fake.frames(TICKET, bet.ticket_id)) == 1
    assert count("risk_messages_sent_total", type="TICKET", attempt="resend") == resent + 1
    assert await money(db, bet.bet_id) == (BetState.ACCEPTED, HoldState.CAPTURED, FUNDS - STAKE)


async def test_final_messages_sent_in_the_loss_window_are_resent_at_the_first_connect(db, fake):
    recent_confirm, recent_decline, old_confirm = [await db(funded_bet) for _ in range(3)]
    # The latest final went out 10 s ago; the window reaches 40 s further back.
    await db(force, recent_confirm.ticket_id, state=State.CONFIRMED, last_sent_at=ago(10))
    await db(force, recent_decline.ticket_id, state=State.DECLINED, last_sent_at=ago(45))
    await db(force, old_confirm.ticket_id, state=State.CONFIRMED, last_sent_at=ago(55))

    async with running(fake) as app:
        await app.wait(fake.wait_for(CONFIRM, recent_confirm.ticket_id))
        await app.wait(fake.wait_for(DECLINE, recent_decline.ticket_id))
        # The re-send comes before the outbox, so once a new ticket goes out, the re-send is complete.
        sentinel = await db(funded_bet)
        await app.wait(fake.wait_for(TICKET, sentinel.ticket_id))

    assert fake.frames(CONFIRM, old_confirm.ticket_id) == []
    assert await db(state, recent_confirm.ticket_id) == State.CONFIRMED
    assert await db(state, recent_decline.ticket_id) == State.DECLINED


# Deadlines


async def test_provider_silence_retransmits_once_then_compensates_and_cancels_at_the_deadline(db, fake):
    fake.ticket_reply = TicketReply.SILENT
    async with running(fake) as app:
        bet = await db(funded_bet)
        await reaches(app, db, bet.ticket_id, State.SENT)

        await db(force, bet.ticket_id, retransmit_at=ago(1))
        await app.wait(fake.wait_for(TICKET, bet.ticket_id, count=2))

        await db(force, bet.ticket_id, deadline_at=ago(1))
        cancellation = await app.wait(fake.wait_for(CANCEL, bet.ticket_id))
        # The cancellation is sent only from a committed compensation.
        assert await money(db, bet.bet_id) == (BetState.VOIDED, HoldState.RELEASED, FUNDS)
        await reaches(app, db, bet.ticket_id, State.CANCELLED)

    assert cancellation.payload == {"reason": "DEADLINE_EXCEEDED"}
    assert len(fake.frames(TICKET, bet.ticket_id)) == 2
    assert (await db(row, bet.ticket_id)).retransmit_at is None


async def test_a_recommendation_after_the_deadline_cancellation_is_ignored(db, fake):
    bet = await db(funded_bet)
    await db(force, bet.ticket_id, deadline_at=ago(1))
    await db(services.cancel_ticket, PORT, bet.ticket_id, Trigger.DEADLINE_PASSED)
    ignored = ("risk_inbound_ignored_total", {"message": "RECOMMENDATION", "reason": "not_awaiting"})
    before = count(ignored[0], **ignored[1])

    async with running(fake) as app:
        await reaches(app, db, bet.ticket_id, State.CANCELLED)
        await fake.send(fake.recommendation(bet.ticket_id, WireDecision.ACCEPT))
        await app.until(counted(ignored[0], before + 1, **ignored[1]))

    assert await db(state, bet.ticket_id) == State.CANCELLED
    assert fake.frames(CONFIRM, bet.ticket_id) == []
    assert await money(db, bet.bet_id) == (BetState.VOIDED, HoldState.RELEASED, FUNDS)


async def test_a_ticket_whose_compensation_keeps_failing_does_not_hold_back_the_next_expired_one(db):
    stuck, behind = await db(funded_bet), await db(funded_bet)
    await db(force, stuck.ticket_id, deadline_at=ago(2))
    await db(force, behind.ticket_id, deadline_at=ago(1))
    port = FailingVoid(stuck.bet_id, OperationalError("lock timeout"))
    # A batch of one is full of the oldest expired ticket, the one that keeps failing.
    sweeper = Sweeper(
        db,
        Decisions(db, port),
        SweeperSettings(interval=timedelta(milliseconds=10), batch_size=1, retry_failed_after=timedelta(seconds=60)),
    )
    loop = Running(asyncio.create_task(sweeper.run()))
    try:
        await reaches(loop, db, behind.ticket_id, State.CANCEL_PENDING)
    finally:
        loop.task.cancel()
        with suppress(asyncio.CancelledError):
            await loop.task

    assert await db(state, stuck.ticket_id) in AWAITING_RECOMMENDATION
    # Two fundings; the stuck bet's stake is still held, the one behind it was released.
    assert await money(db, stuck.bet_id) == (BetState.PENDING_RISK, HoldState.HELD, 2 * FUNDS - STAKE)


# Provider errors


async def test_a_refused_ticket_is_compensated_and_cancelled(db, fake):
    fake.ticket_reply = TicketReply.REFUSE
    async with running(fake) as app:
        bet = await db(funded_bet)
        cancellation = await app.wait(fake.wait_for(CANCEL, bet.ticket_id))
        await reaches(app, db, bet.ticket_id, State.CANCELLED)

    ticket = await db(row, bet.ticket_id)
    assert cancellation.payload == {"reason": "TICKET_REJECTED"}
    assert (ticket.cancel_reason, ticket.last_error_code) == (CancelReason.TICKET_REJECTED, "TICKET_REJECTED")
    assert await money(db, bet.bet_id) == (BetState.VOIDED, HoldState.RELEASED, FUNDS)


async def test_a_refused_confirm_escalates_with_the_bet_still_accepted(db, fake):
    fake.refuse_confirm = True
    async with running(fake) as app:
        bet = await db(funded_bet)
        await reaches(app, db, bet.ticket_id, State.ESCALATED)

    assert (await db(row, bet.ticket_id)).escalation_reason == EscalationReason.CONFIRM_REJECTED
    assert fake.frames(CANCEL, bet.ticket_id) == []
    assert await money(db, bet.bet_id) == (BetState.ACCEPTED, HoldState.CAPTURED, FUNDS - STAKE)


async def test_a_refused_cancellation_escalates_with_the_bet_still_voided(db, fake):
    fake.ticket_reply = TicketReply.SILENT
    fake.refuse_cancellation = True
    async with running(fake) as app:
        bet = await db(funded_bet)
        await reaches(app, db, bet.ticket_id, State.SENT)
        await db(force, bet.ticket_id, deadline_at=ago(1))
        await reaches(app, db, bet.ticket_id, State.ESCALATED)

    ticket = await db(row, bet.ticket_id)
    assert (ticket.escalation_reason, ticket.cancel_reason) == (
        EscalationReason.CANCEL_REJECTED,
        CancelReason.DEADLINE_EXCEEDED,
    )
    assert await money(db, bet.bet_id) == (BetState.VOIDED, HoldState.RELEASED, FUNDS)


async def test_a_refusal_whose_escalation_fails_transiently_is_retried_and_escalates(db):
    bet = await db(funded_bet)
    await db(force, bet.ticket_id, state=State.CONFIRMED)
    flaky = FlakyRunDb(db, tickets.escalate, failures=2)
    decisions = Decisions(cast(DbRunner, flaky), PORT, escalation_retry_delay=timedelta(milliseconds=10))
    escalations = count("risk_escalations_total", reason="CONFIRM_REJECTED")

    await decisions.escalate(bet.ticket_id, Trigger.CONFIRM_REFUSED, error_code="CONFIRM_TOO_LATE")

    assert await db(state, bet.ticket_id) == State.ESCALATED
    assert count("risk_escalations_total", reason="CONFIRM_REJECTED") == escalations + 1


async def test_an_escalation_that_keeps_failing_gives_up_after_its_attempts_and_is_counted(db):
    bet = await db(funded_bet)
    await db(force, bet.ticket_id, state=State.CONFIRMED)
    flaky = FlakyRunDb(db, tickets.escalate, failures=1_000)
    decisions = Decisions(cast(DbRunner, flaky), PORT, escalation_retry_delay=timedelta(milliseconds=10))
    failures = count("risk_escalation_failures_total", kind="db_error")

    await decisions.escalate(bet.ticket_id, Trigger.CONFIRM_REFUSED, error_code="CONFIRM_TOO_LATE")

    assert flaky.failures == 1_000 - 3
    assert await db(state, bet.ticket_id) == State.CONFIRMED
    assert count("risk_escalation_failures_total", kind="db_error") == failures + 1


async def test_undecodable_frames_unknown_tickets_and_uncorrelated_errors_are_dropped_and_counted(db, fake):
    dropped = count("risk_frames_dropped_total", kind="not_json")
    unknown = count("risk_inbound_ignored_total", message="RECOMMENDATION", reason="not_awaiting")
    uncorrelated = count("risk_inbound_ignored_total", message="ERROR", reason="uncorrelated")
    async with running(fake) as app:
        await app.wait(fake.wait_for_session())
        await fake.send("{not json")
        await fake.send(fake.recommendation(uuid4(), WireDecision.ACCEPT))
        await fake.send(fake.error(None, None, "SOMETHING_BROKE"))
        await app.until(counted("risk_frames_dropped_total", dropped + 1, kind="not_json"))
        await app.until(
            counted("risk_inbound_ignored_total", unknown + 1, message="RECOMMENDATION", reason="not_awaiting")
        )
        await app.until(counted("risk_inbound_ignored_total", uncorrelated + 1, message="ERROR", reason="uncorrelated"))

        bet = await db(funded_bet)
        await reaches(app, db, bet.ticket_id, State.CONFIRMED)


# Failures while applying


async def test_a_transient_apply_failure_leaves_the_ticket_for_the_retransmission(db, fake):
    port = FailingAccept(OperationalError("connection lost"), times=1)
    failures = count("risk_apply_failures_total", kind="db_error")
    async with running(fake, port=port) as app:
        bet = await db(funded_bet)
        await app.until(counted("risk_apply_failures_total", failures + 1, kind="db_error"))
        await reaches(app, db, bet.ticket_id, State.SENT)
        assert await money(db, bet.bet_id) == (BetState.PENDING_RISK, HoldState.HELD, FUNDS - STAKE)

        await db(force, bet.ticket_id, retransmit_at=ago(1))
        await reaches(app, db, bet.ticket_id, State.CONFIRMED)

    assert len(fake.frames(TICKET, bet.ticket_id)) == 2
    assert await money(db, bet.bet_id) == (BetState.ACCEPTED, HoldState.CAPTURED, FUNDS - STAKE)


async def test_a_permanent_apply_failure_compensates_and_cancels(db, fake):
    port = FailingAccept(PermanentApplyError("hold missing"))
    async with running(fake, port=port) as app:
        bet = await db(funded_bet)
        cancellation = await app.wait(fake.wait_for(CANCEL, bet.ticket_id))
        await reaches(app, db, bet.ticket_id, State.CANCELLED)

    assert cancellation.payload == {"reason": "APPLY_FAILED"}
    assert fake.frames(CONFIRM, bet.ticket_id) == []
    assert await money(db, bet.bet_id) == (BetState.VOIDED, HoldState.RELEASED, FUNDS)


async def test_an_unclassified_apply_failure_ends_the_process_and_moves_no_money(db, fake):
    port = FailingAccept(RuntimeError("a bug"))
    async with running(fake, port=port) as app:
        bet = await db(funded_bet)
        with pytest.raises(ExceptionGroup) as failure:
            await app.wait(app.task)

    assert failure.group_contains(RuntimeError, match="a bug")
    assert await db(state, bet.ticket_id) in AWAITING_RECOMMENDATION  # whether the relay's commit got in or not
    assert await money(db, bet.bet_id) == (BetState.PENDING_RISK, HoldState.HELD, FUNDS - STAKE)


# The session


async def test_a_planned_close_of_a_stable_session_reconnects_at_once_and_carries_on(db, fake):
    # Any reconnect but the immediate one would wait far past every wait in this test.
    slow = timedelta(seconds=60)
    stable = timedelta(milliseconds=1)
    async with running(fake, session_stable_after=stable, reconnect_initial=slow, reconnect_max=slow) as app:
        first = await db(funded_bet)
        await reaches(app, db, first.ticket_id, State.CONFIRMED)
        planned = count(ENDED, outcome="planned", inbound_queue_full="false")

        await fake.close_sessions(CLOSE_PLANNED)
        await app.wait(fake.wait_for_session(opened=2))
        second = await db(funded_bet)
        await reaches(app, db, second.ticket_id, State.CONFIRMED)

    assert count(ENDED, outcome="planned", inbound_queue_full="false") == planned + 1


async def test_a_dropped_session_reconnects_and_resends_sent_tickets_and_recent_final_messages(db, fake):
    fake.ticket_reply = TicketReply.SILENT
    async with running(fake) as app:
        waiting, confirmed = await db(funded_bet), await db(funded_bet)
        await app.wait(fake.wait_for(TICKET, confirmed.ticket_id))
        await fake.send(fake.recommendation(confirmed.ticket_id, WireDecision.ACCEPT))
        await app.wait(fake.wait_for(CONFIRM, confirmed.ticket_id))
        await reaches(app, db, confirmed.ticket_id, State.CONFIRMED)
        await reaches(app, db, waiting.ticket_id, State.SENT)

        await fake.close_sessions(CLOSE_SERVER_ERROR)
        await app.wait(fake.wait_for(TICKET, waiting.ticket_id, count=2))
        await app.wait(fake.wait_for(CONFIRM, confirmed.ticket_id, count=2))

    assert fake.sessions_opened == 2
    assert await db(state, waiting.ticket_id) == State.SENT
    assert await db(state, confirmed.ticket_id) == State.CONFIRMED
    assert await money(db, confirmed.bet_id) == (BetState.ACCEPTED, HoldState.CAPTURED, 2 * (FUNDS - STAKE))


async def test_a_refused_token_keeps_the_process_retrying_until_the_provider_accepts_it(db, fake):
    refused = count(ENDED, outcome="auth_failed", inbound_queue_full="false")
    async with running(fake, token="rotated") as app:
        await app.until(counted(ENDED, refused + 2, outcome="auth_failed", inbound_queue_full="false"))
        assert fake.sessions_opened == 0

        fake.token = "rotated"
        bet = await db(funded_bet)
        await reaches(app, db, bet.ticket_id, State.CONFIRMED)


@pytest.mark.parametrize("reject", list(RejectNew))
async def test_a_refused_second_instance_stands_by_and_takes_over_the_first_ones_sent_tickets(db, fake, reject):
    fake.reject_new = reject
    fake.ticket_reply = TicketReply.SILENT
    refused = count(ENDED, outcome="already_connected", inbound_queue_full="false")
    async with running(fake) as first:
        bet = await db(funded_bet)
        await first.wait(fake.wait_for(TICKET, bet.ticket_id))
        await reaches(first, db, bet.ticket_id, State.SENT)

        async with running(fake) as second:
            await second.until(counted(ENDED, refused + 2, outcome="already_connected", inbound_queue_full="false"))
            assert fake.sessions_opened == 1
            fake.ticket_reply = TicketReply.ACCEPT

            await first.stop()
            await second.wait(fake.wait_for(TICKET, bet.ticket_id, count=2))
            await reaches(second, db, bet.ticket_id, State.CONFIRMED)

    assert fake.sessions_opened == 2
    assert await money(db, bet.bet_id) == (BetState.ACCEPTED, HoldState.CAPTURED, FUNDS - STAKE)


async def test_appliers_that_fall_behind_stop_the_reader_and_the_dropped_session_is_labelled_then_recovered(db, fake):
    port = StuckAccept()
    # The first copy holds the only applier; the rest fill the queue and the library's buffer, so the socket is
    # no longer read, pongs included.
    fake.duplicates = 40
    backpressure = {"outcome": "transient", "inbound_queue_full": "true"}
    dropped = count(ENDED, **backpressure)
    # Failing the connection waits for the close handshake, which an unread socket never completes: bound it too.
    keepalive: dict[str, Any] = {
        "ws_ping_interval": timedelta(milliseconds=50),
        "ws_ping_timeout": timedelta(milliseconds=200),
        "ws_close_timeout": timedelta(milliseconds=200),
    }
    async with running(fake, port=port, appliers=1, inbound_queue_size=1, **keepalive) as app:
        bet = await db(funded_bet)
        await app.until(counted(ENDED, dropped + 1, **backpressure))

        port.released.set()
        await reaches(app, db, bet.ticket_id, State.CONFIRMED)

    assert await money(db, bet.bet_id) == (BetState.ACCEPTED, HoldState.CAPTURED, FUNDS - STAKE)
