from collections.abc import Callable
from datetime import timedelta
from io import StringIO
from uuid import UUID

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction

from apps.risk_integration.domain.machine import Trigger
from apps.risk_integration.domain.states import (
    CancelReason,
    Decision,
    DeclineReason,
    EscalationReason,
    MessageRef,
    State,
)
from apps.risk_integration.models import RiskTicket
from apps.risk_integration.persistence import tickets
from apps.risk_integration.tests.tickets_helpers import DEADLINE, LEASE, ago, force, new_ticket, row, state

pytestmark = pytest.mark.django_db

A, B = "relay-a", "relay-b"
ZERO = timedelta(0)


def claimed_ids(claims: list[tickets.ClaimedTicket]) -> set[UUID]:
    return {c.ticket_id for c in claims}


def sent_ticket(*, retransmit_after: timedelta = timedelta(seconds=15)) -> UUID:
    tid = new_ticket()
    tickets.claim_due(owner=A, lease=LEASE, limit=100)
    assert tickets.record_send(tid, State.PENDING_SEND, owner=A, retransmit_after=retransmit_after)
    return tid


def test_migrations_match_the_models():
    call_command("makemigrations", "--check", "--dry-run", stdout=StringIO())


def test_new_ticket_waits_to_be_sent_with_a_deadline_counted_from_creation():
    r = row(new_ticket())
    assert r.state == State.PENDING_SEND
    assert r.deadline_at - r.created_at == DEADLINE
    assert r.last_sent_at is None


def test_claim_takes_every_owed_message_and_due_retransmit_but_nothing_else():
    pending = new_ticket()
    confirm = new_ticket()
    tickets.record_accept(confirm)
    decline = new_ticket()
    tickets.record_decline(decline)
    cancel = new_ticket()
    tickets.cancel(cancel, Trigger.TICKET_REFUSED, error_code="BAD")
    waiting = sent_ticket()
    due = sent_ticket(retransmit_after=ZERO)
    confirmed = new_ticket()
    tickets.record_accept(confirmed)
    tickets.claim_due(owner=A, lease=LEASE, limit=100)
    tickets.record_send(confirmed, State.CONFIRM_PENDING, owner=A, retransmit_after=ZERO)
    RiskTicket.objects.update(locked_by="", locked_until=None)  # undo the leases taken during setup

    claims = tickets.claim_due(owner=B, lease=LEASE, limit=100)

    assert claimed_ids(claims) == {pending, confirm, decline, cancel, due}
    assert waiting not in claimed_ids(claims)
    assert {c.ticket_id: c.state for c in claims}[due] == State.SENT
    assert all(row(c.ticket_id).locked_by == B for c in claims)


def test_a_draining_flush_claims_only_final_messages_owed_never_a_ticket():
    new_ticket()
    sent_ticket(retransmit_after=ZERO)
    confirm, decline, cancel = new_ticket(), new_ticket(), new_ticket()
    tickets.record_accept(confirm)
    tickets.record_decline(decline)
    tickets.cancel(cancel, Trigger.TICKET_REFUSED, error_code="BAD")
    RiskTicket.objects.update(locked_by="", locked_until=None)

    claims = tickets.claim_final_pending(owner=B, lease=LEASE, limit=100)

    assert claimed_ids(claims) == {confirm, decline, cancel}


def test_a_leased_row_is_not_claimed_again():
    new_ticket()
    assert len(tickets.claim_due(owner=A, lease=LEASE, limit=100)) == 1
    assert tickets.claim_due(owner=B, lease=LEASE, limit=100) == []


def test_releasing_a_batch_frees_only_its_own_leases_and_leaves_recorded_rows_alone():
    mine, recorded, other_batch = new_ticket(), new_ticket(), new_ticket()
    tickets.claim_due(owner=A, lease=LEASE, limit=100)
    assert tickets.record_send(recorded, State.PENDING_SEND, owner=A, retransmit_after=LEASE)
    theirs = new_ticket()
    tickets.claim_due(owner=B, lease=LEASE, limit=100)

    assert tickets.release_leases([mine, recorded, theirs], owner=A) == 1

    assert claimed_ids(tickets.claim_due(owner=B, lease=LEASE, limit=100)) == {mine}
    assert row(other_batch).locked_by == A
    assert row(theirs).locked_by == B
    assert state(recorded) == State.SENT


def test_a_ticket_past_its_deadline_is_not_sent_but_left_to_the_sweeper():
    tid = new_ticket()
    force(tid, deadline_at=ago(1))
    assert tickets.claim_due(owner=A, lease=LEASE, limit=100) == []
    assert tickets.find_expired(limit=100) == [tid]


def test_expired_lease_is_claimable_and_the_previous_owner_loses_the_send():
    tid = new_ticket()
    tickets.claim_due(owner=A, lease=LEASE, limit=100)
    force(tid, locked_until=ago(1))

    assert claimed_ids(tickets.claim_due(owner=B, lease=LEASE, limit=100)) == {tid}
    assert not tickets.record_send(tid, State.PENDING_SEND, owner=A, retransmit_after=ZERO)
    assert tickets.record_send(tid, State.PENDING_SEND, owner=B, retransmit_after=ZERO)
    assert state(tid) == State.SENT


def test_ticket_is_retransmitted_once_at_its_retransmit_point():
    tid = sent_ticket(retransmit_after=ZERO)
    assert row(tid).retransmit_at is not None

    claims = tickets.claim_due(owner=A, lease=LEASE, limit=100)
    assert [(c.ticket_id, c.state) for c in claims] == [(tid, State.SENT)]
    assert tickets.record_send(tid, State.SENT, owner=A, retransmit_after=ZERO)

    assert row(tid).retransmit_at is None
    assert state(tid) == State.SENT
    assert tickets.claim_due(owner=A, lease=LEASE, limit=100) == []


@pytest.mark.parametrize(
    "reply",
    [
        tickets.record_accept,
        tickets.record_decline,
        lambda tid: tickets.record_domain_rejection(tid, decline_reason=DeclineReason.LIMIT_EXCEEDED),
        lambda tid: tickets.cancel(tid, Trigger.APPLY_FAILED),
        lambda tid: tickets.cancel(tid, Trigger.TICKET_REFUSED, error_code="BAD"),
    ],
)
def test_reply_that_beats_the_relays_commit_is_applied_and_frees_the_row(reply):
    tid = new_ticket()
    tickets.claim_due(owner=A, lease=LEASE, limit=100)

    assert reply(tid)
    assert row(tid).locked_by == ""
    assert row(tid).locked_until is None
    assert not tickets.record_send(tid, State.PENDING_SEND, owner=A, retransmit_after=ZERO)

    # The message the reply made owed is claimable at once.
    [claim] = tickets.claim_due(owner=A, lease=LEASE, limit=100)
    assert claim.state == state(tid)
    assert tickets.record_send(tid, claim.state, owner=A, retransmit_after=ZERO)


def test_refused_confirm_that_beats_the_relays_commit_escalates_with_the_bet_still_accepted():
    tid = new_ticket()
    tickets.record_accept(tid)
    tickets.claim_due(owner=A, lease=LEASE, limit=100)

    assert tickets.escalate(tid, Trigger.CONFIRM_REFUSED, error_code="CONFIRM_WINDOW_CLOSED")
    assert not tickets.record_send(tid, State.CONFIRM_PENDING, owner=A, retransmit_after=ZERO)

    r = row(tid)
    assert r.state == State.ESCALATED
    assert r.recommendation == Decision.ACCEPT
    assert (r.escalation_reason, r.last_error_code, r.last_error_ref_type) == (
        EscalationReason.CONFIRM_REJECTED,
        "CONFIRM_WINDOW_CLOSED",
        MessageRef.CONFIRM_RECOMMENDATION,
    )


def test_refused_cancellation_escalates_and_keeps_the_cancel_reason():
    tid = new_ticket()
    tickets.cancel(tid, Trigger.APPLY_FAILED)
    tickets.claim_due(owner=A, lease=LEASE, limit=100)
    tickets.record_send(tid, State.CANCEL_PENDING, owner=A, retransmit_after=ZERO)

    assert tickets.escalate(tid, Trigger.CANCEL_REFUSED, error_code="ALREADY_SETTLED")

    r = row(tid)
    assert (r.state, r.cancel_reason, r.escalation_reason) == (
        State.ESCALATED,
        CancelReason.APPLY_FAILED,
        EscalationReason.CANCEL_REJECTED,
    )


def test_duplicate_recommendation_is_ignored():
    tid = new_ticket()
    assert tickets.record_accept(tid)
    assert not tickets.record_accept(tid)
    assert not tickets.record_decline(tid)
    assert row(tid).recommendation == Decision.ACCEPT


def test_late_recommendation_after_the_deadline_cancellation_is_ignored():
    tid = sent_ticket()
    force(tid, deadline_at=ago(1))
    assert tickets.cancel(tid, Trigger.DEADLINE_PASSED)

    assert not tickets.record_accept(tid)
    assert state(tid) == State.CANCEL_PENDING


def test_deadline_cancellation_is_decided_on_db_time_by_the_update_itself():
    tid = sent_ticket()
    assert not tickets.cancel(tid, Trigger.DEADLINE_PASSED)
    assert tickets.find_expired(limit=100) == []

    force(tid, deadline_at=ago(1))
    assert tickets.find_expired(limit=100) == [tid]
    assert tickets.cancel(tid, Trigger.DEADLINE_PASSED)
    assert row(tid).cancel_reason == CancelReason.DEADLINE_EXCEEDED
    assert tickets.find_expired(limit=100) == []


def test_confirm_and_decline_are_not_swept_at_the_deadline():
    confirm, decline = new_ticket(), new_ticket()
    tickets.record_accept(confirm)
    tickets.record_decline(decline)
    force(confirm, deadline_at=ago(1))
    force(decline, deadline_at=ago(1))

    assert tickets.find_expired(limit=100) == []
    assert not tickets.cancel(confirm, Trigger.DEADLINE_PASSED)


def test_claimed_cancellation_carries_its_reason_and_the_providers_error_from_the_row():
    tid = new_ticket()
    tickets.cancel(tid, Trigger.TICKET_REFUSED, error_code="ODDS_CHANGED")

    [claim] = tickets.claim_due(owner=A, lease=LEASE, limit=100)
    assert claim.cancel_reason == CancelReason.TICKET_REJECTED
    assert (row(tid).last_error_code, row(tid).last_error_ref_type) == ("ODDS_CHANGED", MessageRef.TICKET)


def test_claimed_domain_rejection_carries_its_decline_reason():
    tid = new_ticket()
    tickets.record_domain_rejection(tid, decline_reason=DeclineReason.LIMIT_EXCEEDED)
    [claim] = tickets.claim_due(owner=A, lease=LEASE, limit=100)
    assert (claim.state, claim.decline_reason) == (State.DECLINE_PENDING, "LIMIT_EXCEEDED")


def test_resend_after_reconnect_pages_through_every_sent_ticket_once():
    ids = {sent_ticket() for _ in range(3)}
    other = new_ticket()
    for tid in ids:
        force(tid, last_sent_at=ago(5))
    since = tickets.db_now()

    seen: list[UUID] = []
    for _ in range(len(ids) + 1):  # bounded: a paging bug must fail here, not spin until a real deadline
        claims = tickets.claim_sent_for_resend(owner=A, lease=LEASE, limit=2, since=since)
        if not claims:
            break
        for c in claims:
            assert c.state == State.SENT
            assert tickets.record_send(c.ticket_id, c.state, owner=A, retransmit_after=ZERO)
        seen += [c.ticket_id for c in claims]
    else:
        pytest.fail("re-send paging did not run dry")

    assert sorted(seen) == sorted(ids)
    assert other not in seen


def test_final_messages_sent_within_the_loss_window_are_resent_whatever_their_type():
    def closed(prepare: Callable[[UUID], object], sent_from: State, sent_ago: float) -> UUID:
        tid = new_ticket()
        prepare(tid)
        tickets.claim_due(owner=A, lease=LEASE, limit=100)
        tickets.record_send(tid, sent_from, owner=A, retransmit_after=ZERO)
        force(tid, last_sent_at=ago(sent_ago))
        return tid

    confirmed = closed(tickets.record_accept, State.CONFIRM_PENDING, 10)
    declined = closed(tickets.record_decline, State.DECLINE_PENDING, 10)
    cancelled = closed(lambda t: tickets.cancel(t, Trigger.APPLY_FAILED), State.CANCEL_PENDING, 10)
    too_old = closed(tickets.record_accept, State.CONFIRM_PENDING, 100)
    since = tickets.db_now()
    window_start = since - timedelta(seconds=40)

    claims = tickets.claim_finals_for_resend(owner=A, lease=LEASE, limit=100, window_start=window_start, since=since)
    assert claimed_ids(claims) == {confirmed, declined, cancelled}
    assert too_old not in claimed_ids(claims)
    for c in claims:
        assert tickets.record_send(c.ticket_id, c.state, owner=A, retransmit_after=ZERO)
        assert state(c.ticket_id) == c.state

    again = tickets.claim_finals_for_resend(owner=A, lease=LEASE, limit=100, window_start=window_start, since=since)
    assert again == []


def test_the_loss_window_of_a_new_process_ends_at_the_latest_final_send_and_ignores_open_tickets():
    assert tickets.last_final_sent_at() is None
    older, latest, still_sent = new_ticket(), new_ticket(), sent_ticket()
    force(older, state=State.CONFIRMED, last_sent_at=ago(600))
    force(latest, state=State.DECLINED, last_sent_at=ago(300))
    force(still_sent, last_sent_at=ago(0))

    assert tickets.last_final_sent_at() == row(latest).last_sent_at


def test_database_refuses_a_cancellation_without_a_reason():
    tid = new_ticket()
    with pytest.raises(IntegrityError), transaction.atomic():
        force(tid, state=State.CANCEL_PENDING)
