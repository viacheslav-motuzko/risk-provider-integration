import random
from datetime import timedelta

import pytest

from apps.risk_integration.protocol.close_codes import SessionOutcome
from apps.risk_integration.runtime.backoff import Backoff, BackoffPolicy

POLICY = BackoffPolicy(
    retry_initial=timedelta(seconds=1),
    retry_max=timedelta(seconds=8),
    standby_retry=timedelta(seconds=2),
    displaced=timedelta(seconds=60),
    auth_initial=timedelta(seconds=30),
    auth_max=timedelta(seconds=120),
    stable_after=timedelta(seconds=60),
)
STABLE = timedelta(seconds=60)
SHORT = timedelta(seconds=1)
REFUSED = None


class _Jitter(random.Random):
    """Always picks the same end of the jitter range."""

    def __init__(self, *, high: bool) -> None:
        super().__init__()
        self.high = high

    def uniform(self, a: float, b: float) -> float:
        return b if self.high else a


def delays(outcome: SessionOutcome, lasted: list[timedelta | None], *, high: bool = True) -> list[float]:
    backoff = Backoff(POLICY, _Jitter(high=high))
    return [backoff.next_delay(outcome, lasted=d).total_seconds() for d in lasted]


def test_a_planned_close_of_a_stable_session_reconnects_at_once_every_time():
    assert delays(SessionOutcome.PLANNED, [STABLE, STABLE, STABLE]) == [0, 0, 0]


def test_a_planned_close_of_a_short_session_backs_off_like_a_transient_one():
    assert delays(SessionOutcome.PLANNED, [SHORT, SHORT, SHORT]) == [1, 2, 4]


def test_transient_failures_double_up_to_the_cap():
    assert delays(SessionOutcome.TRANSIENT, [REFUSED] * 6) == [1, 2, 4, 8, 8, 8]


def test_a_handshake_alone_does_not_reset_the_streak_but_a_stable_session_does():
    assert delays(SessionOutcome.TRANSIENT, [REFUSED, SHORT, SHORT, STABLE, SHORT]) == [1, 2, 4, 1, 2]


def test_a_standby_retries_soon_and_never_slower():
    assert delays(SessionOutcome.ALREADY_CONNECTED, [REFUSED] * 50)[-1] == 2


def test_a_displaced_session_waits_long_before_it_reconnects():
    assert delays(SessionOutcome.DISPLACED, [SHORT, STABLE]) == [60, 60]


def test_a_refused_token_waits_long_and_longer_up_to_the_cap():
    assert delays(SessionOutcome.AUTH_FAILED, [REFUSED] * 4) == [30, 60, 120, 120]


def test_a_new_outcome_starts_a_new_streak():
    backoff = Backoff(POLICY, _Jitter(high=True))
    for _ in range(40):
        backoff.next_delay(SessionOutcome.ALREADY_CONNECTED, lasted=REFUSED)
    # The standby took over; its first drop is a first failure, not the 41st.
    assert backoff.next_delay(SessionOutcome.TRANSIENT, lasted=SHORT) == timedelta(seconds=1)


@pytest.mark.parametrize("outcome", [o for o in SessionOutcome if o is not SessionOutcome.PLANNED])
def test_jitter_takes_off_at_most_half_and_never_reconnects_at_once(outcome):
    assert delays(outcome, [REFUSED], high=False)[0] == delays(outcome, [REFUSED])[0] / 2
