import pytest

from apps.risk_integration.protocol.close_codes import SessionOutcome, classify_close, classify_handshake_rejection


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        (4000, SessionOutcome.PLANNED),
        (4001, SessionOutcome.AUTH_FAILED),
        (4002, SessionOutcome.DISPLACED),
        (4003, SessionOutcome.ALREADY_CONNECTED),
        (None, SessionOutcome.TRANSIENT),
        (1000, SessionOutcome.TRANSIENT),
        (1001, SessionOutcome.TRANSIENT),
        (1006, SessionOutcome.TRANSIENT),
        (1008, SessionOutcome.TRANSIENT),
        (1011, SessionOutcome.TRANSIENT),
        (4999, SessionOutcome.TRANSIENT),
    ],
)
def test_close_after_handshake_is_classified(code, outcome):
    assert classify_close(code) is outcome


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (401, SessionOutcome.AUTH_FAILED),
        (403, SessionOutcome.AUTH_FAILED),
        (409, SessionOutcome.ALREADY_CONNECTED),
        (400, SessionOutcome.TRANSIENT),
        (404, SessionOutcome.TRANSIENT),
        (429, SessionOutcome.TRANSIENT),
        (500, SessionOutcome.TRANSIENT),
        (503, SessionOutcome.TRANSIENT),
    ],
)
def test_refused_upgrade_is_classified(status, outcome):
    assert classify_handshake_rejection(status) is outcome


def test_session_conflict_is_recognised_both_at_upgrade_and_as_close_code():
    # gotchas.md: a conflict can surface either way, and both must lead to the same reconnect policy.
    assert classify_handshake_rejection(409) is classify_close(4003) is SessionOutcome.ALREADY_CONNECTED


def test_kick_out_is_distinct_from_reject_new():
    # Displaced means another session won (long backoff); already-connected means we lost (short backoff).
    assert classify_close(4002) is not classify_close(4003)
