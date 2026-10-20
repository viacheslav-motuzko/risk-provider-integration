"""The transition table of `docs/kb/ticket-lifecycle.md`. Persistence builds every conditional update from it."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from apps.risk_integration.domain.states import (
    AWAITING_RECOMMENDATION,
    CLOSED_FINAL,
    CancelReason,
    EscalationReason,
    MessageRef,
    State,
)


class Trigger(StrEnum):
    TICKET_SENT = "TICKET_SENT"
    TICKET_RESENT = "TICKET_RESENT"
    ACCEPT_APPLIED = "ACCEPT_APPLIED"
    DECLINE_APPLIED = "DECLINE_APPLIED"
    DOMAIN_REJECTED = "DOMAIN_REJECTED"  # ACCEPT, but our own rules reject the bet
    APPLY_FAILED = "APPLY_FAILED"  # a permanent failure while applying the recommendation
    DEADLINE_PASSED = "DEADLINE_PASSED"
    TICKET_REFUSED = "TICKET_REFUSED"
    CONFIRM_SENT = "CONFIRM_SENT"
    DECLINE_SENT = "DECLINE_SENT"
    CANCEL_SENT = "CANCEL_SENT"
    CONFIRM_REFUSED = "CONFIRM_REFUSED"
    CANCEL_REFUSED = "CANCEL_REFUSED"
    FINAL_RESENT = "FINAL_RESENT"  # re-send of a final message after a reconnect, state unchanged


@dataclass(frozen=True)
class Rule:
    sources: frozenset[State]
    target: State | None  # None: the state does not change
    by_lease_owner: bool  # the relay's send transitions apply only for the current lease owner


# Reply-driven rules also accept the state before the send they answer (PENDING_SEND, CONFIRM_PENDING,
# CANCEL_PENDING): a reply can beat the relay's commit of its own send.
RULES: Mapping[Trigger, Rule] = MappingProxyType(
    {
        Trigger.TICKET_SENT: Rule(frozenset({State.PENDING_SEND}), State.SENT, by_lease_owner=True),
        Trigger.TICKET_RESENT: Rule(frozenset({State.SENT}), State.SENT, by_lease_owner=True),
        Trigger.ACCEPT_APPLIED: Rule(AWAITING_RECOMMENDATION, State.CONFIRM_PENDING, by_lease_owner=False),
        Trigger.DECLINE_APPLIED: Rule(AWAITING_RECOMMENDATION, State.DECLINE_PENDING, by_lease_owner=False),
        Trigger.DOMAIN_REJECTED: Rule(AWAITING_RECOMMENDATION, State.DECLINE_PENDING, by_lease_owner=False),
        Trigger.APPLY_FAILED: Rule(AWAITING_RECOMMENDATION, State.CANCEL_PENDING, by_lease_owner=False),
        Trigger.DEADLINE_PASSED: Rule(AWAITING_RECOMMENDATION, State.CANCEL_PENDING, by_lease_owner=False),
        Trigger.TICKET_REFUSED: Rule(AWAITING_RECOMMENDATION, State.CANCEL_PENDING, by_lease_owner=False),
        Trigger.CONFIRM_SENT: Rule(frozenset({State.CONFIRM_PENDING}), State.CONFIRMED, by_lease_owner=True),
        Trigger.DECLINE_SENT: Rule(frozenset({State.DECLINE_PENDING}), State.DECLINED, by_lease_owner=True),
        Trigger.CANCEL_SENT: Rule(frozenset({State.CANCEL_PENDING}), State.CANCELLED, by_lease_owner=True),
        Trigger.CONFIRM_REFUSED: Rule(
            frozenset({State.CONFIRM_PENDING, State.CONFIRMED}), State.ESCALATED, by_lease_owner=False
        ),
        Trigger.CANCEL_REFUSED: Rule(
            frozenset({State.CANCEL_PENDING, State.CANCELLED}), State.ESCALATED, by_lease_owner=False
        ),
        Trigger.FINAL_RESENT: Rule(CLOSED_FINAL, None, by_lease_owner=True),
    }
)

_SEND_TRIGGERS: Mapping[State, Trigger] = MappingProxyType(
    {
        State.PENDING_SEND: Trigger.TICKET_SENT,
        State.SENT: Trigger.TICKET_RESENT,
        State.CONFIRM_PENDING: Trigger.CONFIRM_SENT,
        State.DECLINE_PENDING: Trigger.DECLINE_SENT,
        State.CANCEL_PENDING: Trigger.CANCEL_SENT,
        **dict.fromkeys(CLOSED_FINAL, Trigger.FINAL_RESENT),
    }
)

CANCEL_REASONS: Mapping[Trigger, CancelReason] = MappingProxyType(
    {
        Trigger.APPLY_FAILED: CancelReason.APPLY_FAILED,
        Trigger.DEADLINE_PASSED: CancelReason.DEADLINE_EXCEEDED,
        Trigger.TICKET_REFUSED: CancelReason.TICKET_REJECTED,
    }
)

ESCALATION_REASONS: Mapping[Trigger, EscalationReason] = MappingProxyType(
    {
        Trigger.CONFIRM_REFUSED: EscalationReason.CONFIRM_REJECTED,
        Trigger.CANCEL_REFUSED: EscalationReason.CANCEL_REJECTED,
    }
)

_ERROR_TRIGGERS: Mapping[MessageRef, Trigger] = MappingProxyType(
    {
        MessageRef.TICKET: Trigger.TICKET_REFUSED,
        MessageRef.CONFIRM_RECOMMENDATION: Trigger.CONFIRM_REFUSED,
        MessageRef.REQUEST_CANCELLATION: Trigger.CANCEL_REFUSED,
    }
)

# Which of our messages an error-driven trigger answers; persisted as `last_error_ref_type`.
ERROR_REFS: Mapping[Trigger, MessageRef] = MappingProxyType({t: ref for ref, t in _ERROR_TRIGGERS.items()})


def next_state(state: State, trigger: Trigger) -> State | None:
    """The state after `trigger`, or None if the trigger does not apply in `state` (ignored, logged, counted)."""
    rule = RULES[trigger]
    if state not in rule.sources:
        return None
    return state if rule.target is None else rule.target


def send_trigger(state: State) -> Trigger:
    """The transition the relay commits after sending the message owed in `state`."""
    try:
        return _SEND_TRIGGERS[state]
    except KeyError:
        raise ValueError(f"nothing is sent in state {state}") from None


def trigger_for_error(ref: MessageRef | None, *, unknown_ticket: bool) -> Trigger | None:
    """The trigger for a provider error about one of our messages, or None if the error changes nothing."""
    if ref is None or ref is MessageRef.DECLINE_RECOMMENDATION:
        return None  # ASSUMPTION: a refused decline leaves the ticket not accepted anyway
    if ref is MessageRef.REQUEST_CANCELLATION and unknown_ticket:
        return None  # ASSUMPTION: the provider never knew the ticket, so the cancellation is done
    return _ERROR_TRIGGERS[ref]
