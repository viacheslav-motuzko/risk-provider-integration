# CLAUDE.md

Integration with a WebSocket risk-management provider for sports betting, built as a separate process (a Django management command) inside a Django monolith with a synchronous ORM.

## What good means

- Every decision is defensible: trade-offs, failure modes (restart, duplicates, late messages, partial failure), explicit handling of ambiguity, production-readiness. Volume of code or documentation does not count.
- Prefer a smaller system with tested invariants over a broad one shown on the happy path. Stub what is not essential and say so.

## Working on this repository

- Decisions live in `docs/kb/decisions.md` (D1-D13: context, decision, alternatives, consequences). A change that materializes a decision updates its entry in the same diff. There are no separate ADR files.
- The fixed invariants below are not changed silently. If you think one is wrong, say so before changing code.
- Behaviour the specification leaves open is an assumption: the question and the most probable answer go to `docs/PROVIDER_QUESTIONS.md` (provider A-xx, business B-xx, environment E-xx), and the code relying on a provider guess is marked `# ASSUMPTION:`.
- A new invariant comes with a test that fails when the invariant is broken; check it by breaking the code once.

## Fixed invariants (rationale: `docs/kb/decisions.md`, D1-D12)

1. The DB is the source of truth. Decide, commit, then send. The ticket table is the outbox. Nothing that matters after a restart lives only in memory.
2. `TICKET` is at-least-once: retransmit once before the deadline and after every reconnect. Final messages have no ack (A-02): sent once, re-sent after a reconnect if sent within the loss window, whatever their type (D11). Correctness comes from provider-side idempotency by `ticket_id` (A-03) and compare-and-swap transitions on our side.
3. Claim work with `SELECT ... FOR UPDATE SKIP LOCKED` in a short transaction plus a lease. Never hold row locks across network I/O. Every transition is a conditional update. Duplicate, late and unknown messages are ignored, logged and counted.
4. Waiting is a DB state (`SENT` + per-ticket `deadline_at`), not an in-memory future. One top-level `TaskGroup`, with each session's loops in a nested group that ends with its session; any other task that fails or returns ends the process non-zero. The reader only decodes and enqueues into a bounded queue; appliers do the DB work.
5. The ORM never runs on the event loop. Sync DB work goes through `run_db`: bounded admission, context copied, `SET LOCAL` timeouts. A timed-out `run_db` means "outcome unknown": re-read state before acting.
6. Cancellation: compensate durably first (void the bet, release the held stake, set `CANCEL_PENDING`), then send. Compensation only happens before acceptance; nothing refunds a captured stake. A refused cancellation and a refused or late confirm go to `ESCALATED` (B-01, B-03).
7. Liveness never depends on the provider or on DB health. Readiness means "can take over", not "holds the session". SIGTERM drains within a bound, then closes the session.
8. No leader election of our own. Every instance connects; the provider's session policy (A-05) decides how many sessions are active, and correctness never depends on it (invariant 3). Replicas 1 by default; more are safe (A-15).
9. Money is `Decimal`. Cross-pod time comparisons run in SQL on DB time; domain code never reads a clock; the provider's `ts` never decides anything.
10. The integration reaches the monolith only through `BetDecisionPort`. `betting` and `wallets` import only `risk_integration.api`. Nothing session-level on DB connections (they may pass through a transaction pooler).

## Conventions

- English everywhere: code, comments, docs, commits. The one exception is `README.md`, written in Russian.
- Module layout, for review: declarations first (module constants, lookup tables, enums, dataclasses, exceptions), then functions and classes top-down, so a reader meets the outer behaviour first and goes deeper going down the file: public entry points first, then the private helpers they call, in call order.
- Comments only where the code cannot say it: a non-obvious decision, trade-off or assumption, in short technical words (`# SKIP LOCKED gives no FIFO order; nothing depends on it`). No IDs of decisions, assumptions, questions or table rows (`D5`, `A-07`, `B-01`, `row 8`) in code: a comment must make sense without the docs, which keep the IDs. A guess about the provider is marked `# ASSUMPTION:` so all of them can be found with grep. No narrating comments, no commented-out code. Docstrings only where the signature does not say enough.
- Full type hints on public functions; ruff and mypy must pass.
- Logging is structured JSON with `ticket_id`, `bet_id`, `connection_id` where applicable. No secrets or personal data.
- Errors: transient versus permanent is explicit. No bare `except`. No swallowed error in a loop without a log line and a metric.
- Tests: pytest against a real PostgreSQL, the fake provider instead of mocked sockets, assertions on invariants rather than coverage.
- Configuration comes from environment variables only (decision D10). Secrets are typed `SecretStr` and never logged. `.env` is a local convenience read by make and docker compose, never by application code. No secrets in the repository.
- Dependencies are pinned in `uv.lock`. Add none without stating why.
- Stay inside the scope of the change. A stub is named `Stub*` or clearly marked and logged at startup. No TODOs in code. No dead code.
- Path-scoped rules in `.claude/rules/` load automatically when you edit matching files.

## Documentation (read when)

| File | Read when |
|------|-----------|
| `docs/kb/decisions.md` | Before changing behaviour in the area a decision covers (D1-D13) |
| `docs/PROVIDER_QUESTIONS.md` | When you add or rely on a provider or business assumption (A-xx, B-xx) or an environment assumption (E-xx) |
| `docs/kb/ticket-lifecycle.md` | Before touching states, transitions, the relay or the sweeper |
| `docs/kb/gotchas.md` | Before touching persistence, runtime or ops code |

## Commands

Python 3.12, Django 5.2 LTS, dependencies and commands through `uv` (`uv run ...`).

- `make check` runs lint, mypy, import contracts and tests.
- `make fmt` formats and applies safe fixes. `make db-up` starts Postgres in docker (tests need it). `make migrate`, `make run`; `make up` and `make demo` for the full stack in docker compose.
- A hook formats Python files after each edit; do not fight it.
- `make help` lists all targets. Do not add dependencies with `uv add` without a stated reason.
