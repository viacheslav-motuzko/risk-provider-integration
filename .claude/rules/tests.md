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
- Assert invariants, not coverage: no lost ticket, no double wallet posting, idempotent duplicates, late recommendation ignored, cancellation only after compensation, restart resumes in-flight tickets through retransmission, a timed-out `run_db` whose transaction still commits is handled, a rejected cancellation ends in `ESCALATED`.
- Concurrency tests use real threads or processes against the real DB (two claimers racing for the same rows, two appliers applying a duplicated recommendation).
- Move time by writing `deadline_at` or `locked_until` in the past or by passing `now`; no real sleeps to wait for deadlines.
- The fake provider is test tooling, not production code. Its behaviors (delay, drop, duplicate, late, silence, reject-new, error replies incl. a rejected cancellation, planned close) are scriptable per scenario. Kick-out is covered by a unit test of the close classification.
- Name tests after the behavior they prove.
