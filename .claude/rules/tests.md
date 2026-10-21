---
paths:
  - "**/tests/**"
  - "tests/**"
  - "tools/fake_provider/**"
---

# Tests and the fake provider

- Tests that touch `run_db`, threads or a second connection use `django_db(transaction=True)`; the default per-test transaction is invisible to other connections. Keep pure unit tests off the database.
- Concurrency tests use real threads against the real database. A test that claims a lock serializes two units must observe the second one waiting (`pg_stat_activity`), not just start both at a barrier.
- Runtime behaviour is tested against `tools/fake_provider` over a real socket, configured through its attributes. It is test tooling, not production code; `__main__.py` runs it for docker compose.
- Move time by writing `deadline_at`, `retransmit_at` or `locked_until` into the past on DB time (`tickets_helpers`), never by sleeping. Every wait is bounded (`WAIT`) and watches the app task, so a crash fails with its own error.
- `test_command_process.py` is the only test that starts the command as a process; keep it the only one.
- A test that runs the command stubs `configure_logging`, which rewires global logging.
- Name tests after the behaviour they prove, and assert invariants: no lost ticket, no double wallet posting, compensation before cancellation, duplicates and late messages ignored.
