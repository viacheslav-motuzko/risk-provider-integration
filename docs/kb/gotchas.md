# Gotchas

Seed list from the design discussion and the independent review; verified against Python 3.12/3.13, Django 5.2, websockets 17 and the Postgres docs, but re-check against the installed versions before relying on one. Extend through the stages: propose new entries in the stage summary, and add them here once the user approves.

## Python and asyncio
- `ThreadPoolExecutor` has an unbounded work queue: `max_workers` limits concurrency, not admission. Bound admission with a semaphore.
- `loop.run_in_executor` does not copy context variables into the worker (`asyncio.to_thread` does). Copy them in `run_db`, or the log context is lost.
- Cancelling or timing out an awaited executor call does not stop the thread. The work, including a commit, can still complete afterwards.
- In a `TaskGroup`, a task that fails cancels its siblings; a task that returns normally cancels nothing. Treat a returning loop as a failure.
- A task that catches and swallows `CancelledError` makes shutdown hang. Re-raise it.

## Django
- `sync_to_async` defaults to `thread_sensitive=True`, which runs all calls on one thread and serializes DB access. Use the dedicated executor behind `run_db`.
- Connections are per thread and are not closed or health-checked outside the request cycle. `close_old_connections()` closes every initialized connection of the calling thread that is obsolete or broken; with `CONN_MAX_AGE=0` that means all of them. Never keep anything session-scoped on a connection.
- `transaction.atomic` does not work across an async boundary. Keep each transaction inside one sync function executed by `run_db`.
- Django raises `SynchronousOnlyOperation` when the ORM is used on a running event loop. Treat it as a guardrail; do not bypass it with `DJANGO_ALLOW_ASYNC_UNSAFE`.
- pytest-django wraps a `django_db` test in one transaction on one connection. `run_db` works in other threads with other connections, which cannot see that uncommitted data. Tests that exercise `run_db`, threads or several processes must use `django_db(transaction=True)` (slower: tables are flushed afterwards). Keep pure unit tests off the database.

## Postgres
- `SELECT ... FOR UPDATE SKIP LOCKED` needs a transaction (`atomic`) and does not exist on SQLite. Claim in a short transaction, write the lease, commit, then do I/O. The row lock ends at commit; after that only the lease condition and CAS protect the row.
- With `ORDER BY` at READ COMMITTED, `SKIP LOCKED` can return rows out of order. Do not rely on FIFO.
- `now()` is the transaction start time, not the statement time. Fine for short transactions; use `clock_timestamp()` if a long one ever matters.
- Behind a transaction-mode pooler, session state does not survive between transactions: no advisory locks, no `LISTEN/NOTIFY`, no session `SET`. Set timeouts with `SET LOCAL` inside the transaction; startup options in the connection string may be rejected by the pooler.
- A connection that died uncleanly is noticed by the server only on its next socket activity or through TCP keepalive (about two hours by default on Linux). Anything that must expire after a crash (ticket leases) keeps its own TTL in a column instead of relying on the connection.

## WebSocket (`websockets`, new asyncio API)
- Use `websockets.asyncio.client` and `.server`; the legacy API is deprecated.
- The incoming queue (`max_queue`, default 16 frames) pauses reading from the transport when full. Pongs are then not read either, and the keepalive (`ping_interval` and `ping_timeout`, 20 s each by default) closes the connection. Keep the reader fast: it only decodes and enqueues.
- `close_timeout` defaults to 10 s. Count it in the shutdown budget.
- A session conflict can surface as an HTTP rejection at the upgrade or as a close frame after the handshake. Classify both.
- A half-open TCP connection looks alive until a ping times out, and the provider holds its side of the session until it notices too (A-05).
- Do not retry connects in a tight loop: use backoff with jitter, and keep a logon-rate ceiling in mind (A-12).

## Kubernetes and ops
- Liveness must not depend on the provider or on DB health; otherwise an outage restarts every pod.
- Readiness of these pods routes no traffic (no Service sends requests to them); it gates rollout progress and PDBs only. If readiness required holding the session, a rolling update would stall.
- RollingUpdate defaults are 25% surge (rounded up) and 25% unavailable (rounded down). Set them explicitly. `Recreate` does not prevent two pods on eviction or manual deletion.
- With an exec-form `CMD`, Python is PID 1 and ignores SIGTERM until a handler is installed. Install it early or use an init process.
- `terminationGracePeriodSeconds` must exceed the drain budget plus the WebSocket close timeout.
- Never label metrics with `ticket_id` or `bet_id`; the cardinality grows without bound.
- During a rolling update two pods overlap. Correctness must not depend on the overlap being absent (invariant 3).

## Tooling
- `uv.lock` is committed. Run `make lock` once at the start to take the newest compatible versions, then freeze; Docker and CI use `uv sync --frozen`.
- `lint-imports` fails if a package named in a contract does not exist, so the scaffold creates every subpackage. It squashes external packages to their root (`django`, not `django.db`).
- `pydantic-settings` pulls in `python-dotenv`. Do not configure `env_file` on settings classes (D10).
- pytest exits with code 5 when no tests are collected; `make test` tolerates that so an empty scaffold passes `make check`.
- make reads `.env` as plain `KEY=VALUE` lines: no inline comments, no shell quoting tricks. Docker compose reads the same file, and its host-side values (`localhost`) are wrong inside containers, so the compose file sets container-side addresses itself.
