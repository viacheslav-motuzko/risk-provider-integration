"""How long to wait before the next connect, by how the last session ended or was refused."""

import random
from dataclasses import dataclass
from datetime import timedelta

from apps.risk_integration.protocol.close_codes import SessionOutcome

_MAX_DOUBLINGS = 16  # every cap is reached long before; keeps the arithmetic bounded


@dataclass(frozen=True)
class BackoffPolicy:
    retry_initial: timedelta
    retry_max: timedelta
    standby_retry: timedelta
    displaced: timedelta
    auth_initial: timedelta
    auth_max: timedelta
    stable_after: timedelta


class Backoff:
    """Endings with the same outcome in a row form a streak, and a growing delay grows with it. A session that
    stayed open for `stable_after`, or a different outcome, starts a new streak. A handshake alone does not: a
    provider that accepts and closes at once must not be reconnected in a tight loop.

    - planned close of a stable session: at once, a routine end of the connection's lifetime;
    - already connected: a short delay that does not grow, so a standby takes over soon after the holder leaves;
    - displaced: long, so two instances do not displace each other in a fast loop;
    - auth failed: long and growing; an auth outage on the provider's side must not become a crash loop;
    - transient, and a planned close of a session that did not last: short and growing.
    """

    def __init__(self, policy: BackoffPolicy, rand: random.Random | None = None) -> None:
        self._policy = policy
        self._rand = rand or random.Random()  # noqa: S311 - jitter, not security
        self._outcome: SessionOutcome | None = None
        self._streak = 0

    def next_delay(self, outcome: SessionOutcome, *, lasted: timedelta | None) -> timedelta:
        """`lasted` is how long the session was open; `None` when it was refused before it opened."""
        stable = lasted is not None and lasted >= self._policy.stable_after
        if stable or outcome != self._outcome:
            self._streak = 0
        self._outcome = outcome
        self._streak += 1
        p = self._policy
        match outcome:
            case SessionOutcome.PLANNED if stable:
                return timedelta(0)
            case SessionOutcome.ALREADY_CONNECTED:
                return self._jittered(p.standby_retry)
            case SessionOutcome.DISPLACED:
                return self._jittered(p.displaced)
            case SessionOutcome.AUTH_FAILED:
                return self._jittered(self._grown(p.auth_initial, p.auth_max))
            case _:
                return self._jittered(self._grown(p.retry_initial, p.retry_max))

    def _grown(self, initial: timedelta, cap: timedelta) -> timedelta:
        return min(cap, initial * (1 << min(self._streak - 1, _MAX_DOUBLINGS)))

    def _jittered(self, delay: timedelta) -> timedelta:
        return delay * self._rand.uniform(0.5, 1.0)
