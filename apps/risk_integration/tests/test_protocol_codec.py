import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from apps.risk_integration.protocol.codec import FrameError, FrameErrorKind, decode, encode
from apps.risk_integration.protocol.messages import (
    CancellationReason,
    ConfirmRecommendation,
    Decision,
    DeclineReason,
    DeclineRecommendation,
    OutboundType,
    ProviderError,
    Recommendation,
    RequestCancellation,
    Ticket,
)

TS = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
TICKET_ID = UUID("6f1c2a9e-8f7b-4f3e-9a51-1d2e3f4a5b6c")


def _ticket(**overrides: object) -> Ticket:
    fields: dict[str, object] = {
        "ticket_id": TICKET_ID,
        "stake": Decimal("10.50"),
        "currency": "EUR",
        "odds": Decimal("1.85"),
        "selection_id": "sel-1",
        "customer_ref": "c-7f3a",
    }
    fields.update(overrides)
    return Ticket.model_validate(fields)


def _frame(type_: str = "RECOMMENDATION", ticket_id: object = str(TICKET_ID), **payload: object) -> str:
    return json.dumps({"type": type_, "ticket_id": ticket_id, "ts": "2026-10-20T12:00:01Z", "payload": payload})


def _decode_error(frame: str | bytes) -> FrameError:
    with pytest.raises(FrameError) as info:
        decode(frame)
    return info.value


# Outbound


def test_ticket_frame_has_envelope_and_exact_money_as_strings():
    frame = json.loads(encode(_ticket(), ts=TS))

    assert frame == {
        "type": "TICKET",
        "ticket_id": str(TICKET_ID),
        "ts": "2026-10-20T12:00:00Z",
        "payload": {
            "stake": "10.50",
            "currency": "EUR",
            "odds": "1.85",
            "selection_id": "sel-1",
            "customer_ref": "c-7f3a",
        },
    }


def test_decimals_round_trip_without_loss():
    stake = Decimal("12345678901234567890.123456789")
    payload = json.loads(encode(_ticket(stake=stake), ts=TS))["payload"]

    assert Decimal(payload["stake"]) == stake


@pytest.mark.parametrize(
    ("message", "expected_type", "expected_payload"),
    [
        (ConfirmRecommendation(ticket_id=TICKET_ID), "CONFIRM_RECOMMENDATION", {}),
        (DeclineRecommendation(ticket_id=TICKET_ID), "DECLINE_RECOMMENDATION", {}),
        (
            DeclineRecommendation(ticket_id=TICKET_ID, reason=DeclineReason.INSUFFICIENT_FUNDS),
            "DECLINE_RECOMMENDATION",
            {"reason": "INSUFFICIENT_FUNDS"},
        ),
        (
            RequestCancellation(ticket_id=TICKET_ID, reason=CancellationReason.DEADLINE_EXCEEDED),
            "REQUEST_CANCELLATION",
            {"reason": "DEADLINE_EXCEEDED"},
        ),
    ],
)
def test_final_messages_encode_type_ticket_and_payload(message, expected_type, expected_payload):
    frame = json.loads(encode(message, ts=TS))

    assert frame["type"] == expected_type
    assert frame["ticket_id"] == str(TICKET_ID)
    assert frame["payload"] == expected_payload


def test_ts_is_sent_in_utc_whatever_the_input_offset():
    local = TS.astimezone(timezone(timedelta(hours=3)))

    assert datetime.fromisoformat(json.loads(encode(ConfirmRecommendation(ticket_id=TICKET_ID), ts=local))["ts"]) == TS


def test_encode_refuses_a_naive_timestamp():
    with pytest.raises(ValidationError):
        encode(ConfirmRecommendation(ticket_id=TICKET_ID), ts=datetime(2026, 10, 20, 12, 0))


@pytest.mark.parametrize(
    "overrides",
    [
        {"stake": Decimal("NaN")},
        {"stake": Decimal("Infinity")},
        {"odds": Decimal("NaN")},
        {"unexpected": "field"},
    ],
)
def test_ticket_refuses_non_finite_money_and_unknown_fields(overrides):
    with pytest.raises(ValidationError):
        _ticket(**overrides)


def test_outbound_messages_are_immutable():
    message = ConfirmRecommendation(ticket_id=TICKET_ID)

    with pytest.raises(ValidationError):
        message.ticket_id = uuid4()  # type: ignore[misc]  # the write the model must refuse


# Inbound


@pytest.mark.parametrize("decision", [Decision.ACCEPT, Decision.DECLINE])
def test_recommendation_decodes_with_envelope_fields(decision):
    message = decode(_frame(decision=decision.value, reason="limits"))

    assert message == Recommendation(
        ticket_id=TICKET_ID, decision=decision, reason="limits", ts=datetime(2026, 10, 20, 12, 0, 1, tzinfo=UTC)
    )


def test_unknown_fields_are_ignored_for_forward_compatibility():
    raw = json.loads(_frame(decision="ACCEPT", score=0.93))
    raw["trace"] = "abc"

    message = decode(json.dumps(raw))

    assert isinstance(message, Recommendation)
    assert message.decision is Decision.ACCEPT


def test_envelope_ticket_id_wins_over_one_inside_the_payload():
    raw = json.loads(_frame(decision="ACCEPT"))
    raw["payload"]["ticket_id"] = str(uuid4())

    message = decode(json.dumps(raw))

    assert message.ticket_id == TICKET_ID


def test_missing_provider_ts_does_not_reject_a_recommendation():
    # The provider's ts never decides anything, so its absence must not cost us the recommendation.
    frame = json.dumps({"type": "RECOMMENDATION", "ticket_id": str(TICKET_ID), "payload": {"decision": "ACCEPT"}})

    message = decode(frame)

    assert isinstance(message, Recommendation)
    assert message.ts is None


def test_error_correlates_by_ticket_and_rejected_message_type():
    message = decode(_frame("ERROR", ref_type="CONFIRM_RECOMMENDATION", code="TOO_LATE", message="window closed"))

    assert message == ProviderError(
        ticket_id=TICKET_ID,
        ref_type=OutboundType.CONFIRM_RECOMMENDATION,
        code="TOO_LATE",
        message="window closed",
        ts=datetime(2026, 10, 20, 12, 0, 1, tzinfo=UTC),
    )
    assert not message.is_unknown_ticket


def test_error_without_ticket_or_ref_type_decodes():
    message = decode(_frame("ERROR", ticket_id=None, code="INVALID_MESSAGE"))

    assert isinstance(message, ProviderError)
    assert message.ticket_id is None
    assert message.ref_type is None


def test_unknown_ticket_error_is_recognised():
    message = decode(_frame("ERROR", ref_type="REQUEST_CANCELLATION", code="UNKNOWN_TICKET"))

    assert isinstance(message, ProviderError)
    assert message.is_unknown_ticket


def test_unrecognised_error_code_is_kept_raw():
    message = decode(_frame("ERROR", ref_type="TICKET", code="SOMETHING_NEW"))

    assert isinstance(message, ProviderError)
    assert message.code == "SOMETHING_NEW"


# Malformed frames: always FrameError with a bounded kind, never another exception.


@pytest.mark.parametrize(
    ("frame", "kind"),
    [
        (b'{"type":"RECOMMENDATION"}', FrameErrorKind.BINARY),
        ("not json", FrameErrorKind.NOT_JSON),
        ("", FrameErrorKind.NOT_JSON),
        ("[" * 100_000, FrameErrorKind.NOT_JSON),
        ("[]", FrameErrorKind.BAD_ENVELOPE),
        ('"RECOMMENDATION"', FrameErrorKind.BAD_ENVELOPE),
        ("{}", FrameErrorKind.BAD_ENVELOPE),
        (json.dumps({"type": "RECOMMENDATION", "ticket_id": "nope", "payload": {}}), FrameErrorKind.BAD_ENVELOPE),
        (json.dumps({"type": "RECOMMENDATION", "payload": []}), FrameErrorKind.BAD_ENVELOPE),
        (
            json.dumps({"type": "RECOMMENDATION", "ts": "2026-10-20T12:00:00", "payload": {}}),
            FrameErrorKind.BAD_ENVELOPE,
        ),
        (_frame("HEARTBEAT"), FrameErrorKind.UNKNOWN_TYPE),
        (_frame("TICKET"), FrameErrorKind.UNKNOWN_TYPE),
        (_frame(decision="MAYBE"), FrameErrorKind.BAD_PAYLOAD),
        (_frame(), FrameErrorKind.BAD_PAYLOAD),
        (_frame(ticket_id=None, decision="ACCEPT"), FrameErrorKind.BAD_PAYLOAD),
        (_frame("ERROR", ref_type="RECOMMENDATION", code="X"), FrameErrorKind.BAD_PAYLOAD),
        (_frame("ERROR", code=""), FrameErrorKind.BAD_PAYLOAD),
    ],
)
def test_malformed_frame_raises_frame_error_of_its_kind(frame, kind):
    assert _decode_error(frame).kind is kind


def test_frame_error_keeps_type_and_ticket_for_the_log_line():
    error = _decode_error(_frame(decision="MAYBE"))

    assert error.message_type == "RECOMMENDATION"
    assert error.ticket_id == TICKET_ID


def test_frame_error_detail_does_not_echo_input_values():
    error = _decode_error(_frame("ERROR", ref_type="customer-secret-value", code="X"))

    assert "ref_type" in error.detail
    assert "customer-secret-value" not in error.detail
