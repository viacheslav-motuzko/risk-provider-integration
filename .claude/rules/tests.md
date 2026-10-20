---
paths:
  - "**/tests/**"
  - "tests/**"
  - "tools/fake_provider/**"
---

# Tests and the fake provider

- Use a real PostgreSQL. SQLite cannot test `SKIP LOCKED` or concurrent conditional updates. Do not mock the database.
- Tests that exercise `run_db`, threads or several processes must use `django_db(transaction=True)`: pytest-django's default per-test transaction is invisible to other connections.
- Test runtime behavior against the fake provider, not mocked websocket objects.
- Assert invariants, not coverage: no lost ticket, no double wallet posting, idempotent duplicates, late recommendation ignored, cancellation only after compensation, restart resumes in-flight tickets through retransmission, a timed-out `run_db` whose transaction still commits is handled, a refused cancellation ends in `ESCALATED`, a refused confirm ends in `ESCALATED` with the bet still accepted, a reply that arrives before the relay commits its send is applied.
- Concurrency tests use real threads or processes against the real DB (two claimers racing for the same rows, two appliers applying a duplicated recommendation).
- Move time by writing `deadline_at` or `locked_until` in the past or by passing `now`; no real sleeps to wait for deadlines.
- The fake provider is test tooling, not production code. It runs in-process from pytest with a few knobs per scenario: recommend, silence, duplicate, error replies (incl. a refused confirm and a refused cancellation), reject-new, close. No cross-process e2e suite. Kick-out is covered by a unit test of the close classification.
- Name tests after the behavior they prove.
