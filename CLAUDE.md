# CLAUDE.md

Integration with a WebSocket risk-management provider for sports betting, built as a separate process (a Django management command) inside a Django monolith with a synchronous ORM.

Private context lives in `CLAUDE.local.md` and `docs/kb/private/`. Those files are gitignored. Never copy their content into committed files.

## What good means

- The work is reviewed by engineers and discussed afterwards, so every decision must be defensible: trade-offs, failure modes (restart, duplicates, late messages, partial failure), explicit handling of ambiguity, production-readiness. Volume of code or documentation does not count.
- Prefer a smaller system with tested invariants over a broad one shown on the happy path. Stub what is not essential and say so.

## Process: stages and gates

The stage table is in `docs/kb/stages.md`. Work on one stage at a time and stop at every gate.

- **One stage = one session.** Sessions do not share context. State lives in the repository: `docs/kb/progress.md` (handoff), `docs/kb/decisions.md`, git history. Do not rely on anything from an earlier session that is not written there.
- **Start of a stage:** read `docs/kb/progress.md`, then confirm the previous stage branch is merged into `main` (`git branch --merged main`) and the working tree is clean (`git status`); if not, stop and say so. Read the files in that stage's `read:` column, then post a design note (at most one screen): what you will build; real design choices with 2-3 options, trade-offs and a recommendation. If a real choice exists, STOP and wait for the decision; otherwise proceed.
- **During:** implement, test, lint. Keep the diff reviewable (about 600 lines of non-test code at most). Record a decision in `docs/kb/decisions.md` in the stage where it materializes (context, decision, alternatives, consequences). There are no separate ADR files.
- **End:** run `make check` and `make mutate` (add an entry to `tools/mutation_check.py` for every invariant the stage introduces; a sabotage that survives is a missing test, fixed before the summary), write the summary into `docs/kb/progress.md` (done / deviations / assumptions added / open questions / proposed gotchas / how to review incl. the `make mutate` table / proposed commit message), post the same summary, then STOP. Nothing is staged or committed: the user reviews the full diff of the stage first. Start the next stage only on an explicit go.
- A flaw in an earlier stage is fixed in the current stage and called out. Never rewrite earlier work silently. Git: at the start of a stage create `stage/N-short-name` from the current `main` and work only there.
- **Review before commit.** The whole stage stays uncommitted in the working tree until the user has reviewed its full diff (`git status`, `git diff`, new files). Never stage or commit on your own. Fixes from the review go into the working tree, still uncommitted. Commits are made after the review, by the user or by you on an explicit instruction, as one commit per stage with the message proposed in the summary (`stage N: short name`). Never merge, rebase, reset or push: merging into `main` is the user's gate action.
- The fixed decisions below are not changed silently. If you think one is wrong, say so at the next gate.
- The provider cannot be asked. Write the question and the most probable answer yourself (`docs/kb/provider-assumptions.md`). Ask the user about everything else.
- Code-level patterns (layers, ports, Django practices, DDD, persistence style, transaction boundaries) are agreed with the user when a stage touches them: `docs/kb/design-topics.md`.

## Fixed invariants (rationale: `docs/kb/decisions.md`, D1-D12)

1. The DB is the source of truth. Decide, commit, then send. The ticket table is the outbox. Nothing that matters after a restart lives only in memory.
2. `TICKET` is at-least-once: retransmit once before the deadline and after every reconnect. Final messages have no ack (A-02): sent once, re-sent after a reconnect if sent within the loss window, whatever their type (D11). Correctness comes from provider-side idempotency by `ticket_id` (A-03) and compare-and-swap transitions on our side.
3. Claim work with `SELECT ... FOR UPDATE SKIP LOCKED` in a short transaction plus a lease. Never hold row locks across network I/O. Every transition is a conditional update. Duplicate, late and unknown messages are ignored, logged and counted.
4. Waiting is a DB state (`SENT` + per-ticket `deadline_at`), not an in-memory future. One top-level `TaskGroup`, with each session's loops in a nested group that ends with its session; any other task that fails or returns ends the process non-zero. The reader only decodes and enqueues into a bounded queue; appliers do the DB work.
5. The ORM never runs on the event loop. Sync DB work goes through `run_db`: bounded admission, context copied, `SET LOCAL` timeouts. A timed-out `run_db` means "outcome unknown": re-read state before acting.
6. Cancellation: compensate durably first (void the bet, release the held stake, set `CANCEL_PENDING`), then send. Compensation only happens before acceptance; nothing refunds a captured stake. A refused cancellation and a refused or late confirm go to `ESCALATED` (B-01, B-03).
7. Liveness never depends on the provider or on DB health. Readiness means "can take over", not "holds the session". SIGTERM drains within a bound, then closes the session.
8. No leader election of our own. Every instance connects; the provider's session policy (A-05) decides how many sessions are active, and correctness never depends on it (invariant 3). Replicas 1 by default; more are safe (A-15).
9. Money is `Decimal`. Cross-pod time comparisons run in SQL on DB time; domain code receives `now` as an argument; the provider's `ts` never decides anything.
10. The integration reaches the monolith only through `BetDecisionPort`. `betting` and `wallets` import only `risk_integration.api`. Nothing session-level on DB connections (they may pass through a transaction pooler).

## Conventions

- English everywhere: code, comments, docs, commits. The one exception is `README.md`, written in Russian from stage 6.
- Module layout, for review: declarations first (module constants, lookup tables, enums, dataclasses, exceptions), then functions and classes top-down, so a reader meets the outer behaviour first and goes deeper going down the file: public entry points first, then the private helpers they call, in call order.
- Comments only where the code cannot say it: a non-obvious decision, trade-off or assumption, in short technical words (`# SKIP LOCKED gives no FIFO order; nothing depends on it`). No IDs of decisions, assumptions, questions or table rows (`D5`, `A-07`, `B-01`, `row 8`) in code: a comment must make sense without the docs, which keep the IDs. A guess about the provider is marked `# ASSUMPTION:` so all of them can be found with grep. No narrating comments, no commented-out code. Docstrings only where the signature does not say enough.
- Full type hints on public functions; ruff and the type checker from the scaffold must pass.
- Logging is structured JSON with `ticket_id`, `bet_id`, `connection_id` where applicable. No secrets or personal data.
- Errors: transient versus permanent is explicit. No bare `except`. No swallowed error in a loop without a log line and a metric.
- Tests: pytest against a real PostgreSQL, the fake provider instead of mocked sockets, assertions on invariants rather than coverage.
- Configuration comes from environment variables only (decision D10). Secrets are typed `SecretStr` and never logged. `.env` is a local convenience read by make and docker compose, never by application code. No secrets in the repository.
- Dependencies are pinned by the scaffold. Add none without stating why in the stage summary.
- Stay inside the stage scope. A stub is named `Stub*` or clearly marked and logged at startup. No TODOs outside README "next steps". No dead code.
- Path-scoped rules in `.claude/rules/` load automatically when you edit matching files.
- The repository may be public. Never put the private context into committed files.

## Knowledge base (read when)

| File | Read when |
|------|-----------|
| `docs/kb/progress.md` | Start of every stage (handoff from the previous session) |
| `docs/kb/stages.md` | Start of every stage |
| `docs/kb/decisions.md` | Before stage 1, and whenever a stage touches the area (D1-D12) |
| `docs/kb/provider-assumptions.md` | Stage 1, and whenever you add or rely on an assumption (provider A-xx, business B-xx, environment E-xx) |
| `docs/kb/ticket-lifecycle.md` | Stages 2-4, 6 |
| `docs/kb/design-topics.md` | Whenever a design note contains a real choice |
| `docs/kb/gotchas.md` | Before touching persistence, runtime or ops code |
| `docs/PROJECT_STRUCTURE.md` | Start of stage 1, and whenever you add a module |

If the repository scaffold differs from `docs/PROJECT_STRUCTURE.md`, the repository wins: adapt and flag the difference in your next summary.

## Commands

Python 3.12, Django 5.2 LTS, dependencies and commands through `uv` (`uv run ...`).

- `make check` runs lint, mypy, import contracts and tests. It must pass before every stage summary.
- `make fmt` formats and applies safe fixes. `make db-up` starts Postgres in docker (tests need it). `make migrate`, `make run`.
- A hook formats Python files after each edit; do not fight it.
- `make help` lists all targets. Do not add dependencies with `uv add` without a stated reason.
