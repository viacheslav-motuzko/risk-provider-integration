from itertools import product

import pytest

from apps.risk_integration.domain.machine import (
    CANCEL_REASONS,
    ESCALATION_REASONS,
    RULES,
    Trigger,
    next_state,
    send_trigger,
    trigger_for_error,
)
from apps.risk_integration.domain.states import AWAITING_RECOMMENDATION, CLOSED_FINAL, OUTBOX, MessageRef, State

S, T = State, Trigger

ALLOWED = {
    (T.TICKET_SENT, S.PENDING_SEND): S.SENT,
    (T.TICKET_RESENT, S.SENT): S.SENT,
    (T.ACCEPT_APPLIED, S.PENDING_SEND): S.CONFIRM_PENDING,
    (T.ACCEPT_APPLIED, S.SENT): S.CONFIRM_PENDING,
    (T.DECLINE_APPLIED, S.PENDING_SEND): S.DECLINE_PENDING,
    (T.DECLINE_APPLIED, S.SENT): S.DECLINE_PENDING,
    (T.DOMAIN_REJECTED, S.PENDING_SEND): S.DECLINE_PENDING,
    (T.DOMAIN_REJECTED, S.SENT): S.DECLINE_PENDING,
    (T.APPLY_FAILED, S.PENDING_SEND): S.CANCEL_PENDING,
    (T.APPLY_FAILED, S.SENT): S.CANCEL_PENDING,
    (T.DEADLINE_PASSED, S.PENDING_SEND): S.CANCEL_PENDING,
    (T.DEADLINE_PASSED, S.SENT): S.CANCEL_PENDING,
    (T.TICKET_REFUSED, S.PENDING_SEND): S.CANCEL_PENDING,
    (T.TICKET_REFUSED, S.SENT): S.CANCEL_PENDING,
    (T.CONFIRM_SENT, S.CONFIRM_PENDING): S.CONFIRMED,
    (T.DECLINE_SENT, S.DECLINE_PENDING): S.DECLINED,
    (T.CANCEL_SENT, S.CANCEL_PENDING): S.CANCELLED,
    (T.CONFIRM_REFUSED, S.CONFIRM_PENDING): S.ESCALATED,
    (T.CONFIRM_REFUSED, S.CONFIRMED): S.ESCALATED,
    (T.CANCEL_REFUSED, S.CANCEL_PENDING): S.ESCALATED,
    (T.CANCEL_REFUSED, S.CANCELLED): S.ESCALATED,
    (T.FINAL_RESENT, S.CONFIRMED): S.CONFIRMED,
    (T.FINAL_RESENT, S.DECLINED): S.DECLINED,
    (T.FINAL_RESENT, S.CANCELLED): S.CANCELLED,
}


@pytest.mark.parametrize(("trigger", "state"), list(product(Trigger, State)))
def test_transition_table_matches_the_lifecycle_spec(trigger, state):
    assert next_state(state, trigger) == ALLOWED.get((trigger, state))


def test_money_moving_transitions_leave_only_pre_acceptance_states():
    # D7: compensation and decline release a held stake, which exists only before acceptance.
    for rule in RULES.values():
        if rule.target in {S.CANCEL_PENDING, S.DECLINE_PENDING}:
            assert rule.sources <= AWAITING_RECOMMENDATION


def test_every_cancel_and_escalation_persists_a_reason():
    assert {t for t, r in RULES.items() if r.target is S.CANCEL_PENDING} == set(CANCEL_REASONS)
    assert {t for t, r in RULES.items() if r.target is S.ESCALATED} == set(ESCALATION_REASONS)


@pytest.mark.parametrize("state", sorted(OUTBOX | {S.SENT} | CLOSED_FINAL))
def test_every_claimable_state_has_the_send_transition_of_its_own_message(state):
    trigger = send_trigger(state)
    assert RULES[trigger].by_lease_owner
    assert next_state(state, trigger) is not None


def test_only_send_transitions_require_the_lease_owner():
    sends = {send_trigger(s) for s in OUTBOX | {S.SENT} | CLOSED_FINAL}
    assert {t for t, r in RULES.items() if r.by_lease_owner} == sends


def test_nothing_is_sent_for_an_escalated_ticket():
    with pytest.raises(ValueError, match="ESCALATED"):
        send_trigger(S.ESCALATED)


@pytest.mark.parametrize(
    ("ref", "unknown_ticket", "trigger"),
    [
        (MessageRef.TICKET, False, T.TICKET_REFUSED),
        (MessageRef.CONFIRM_RECOMMENDATION, False, T.CONFIRM_REFUSED),
        (MessageRef.CONFIRM_RECOMMENDATION, True, T.CONFIRM_REFUSED),
        (MessageRef.REQUEST_CANCELLATION, False, T.CANCEL_REFUSED),
        (MessageRef.REQUEST_CANCELLATION, True, None),
        (MessageRef.DECLINE_RECOMMENDATION, False, None),
        (None, False, None),
    ],
)
def test_provider_error_maps_to_a_trigger_or_is_ignored(ref, unknown_ticket, trigger):
    assert trigger_for_error(ref, unknown_ticket=unknown_ticket) is trigger
