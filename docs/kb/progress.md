# Progress and handoff

Written by the agent at the end of every stage and read at the start of the next one (sessions do not share context). Keep it short and factual. Folded into the README or removed in stage 7.

## Status

| Stage | Branch | State | Merged into main |
|-------|--------|-------|------------------|
| 0. Bootstrap | `stage/0-bootstrap` | done; scaffold and stage 0 docs in one commit by the user | no |
| 1. Provider contract | `stage/1-protocol` | not started | no |
| 2. Domain and persistence | `stage/2-domain-persistence` | not started | no |
| 3. Monolith side | `stage/3-monolith` | not started | no |
| 4. Runtime core | `stage/4-runtime` | not started | no |
| 5. Operability | `stage/5-ops` | not started | no |
| 6. Fake provider and failures | `stage/6-fake-provider` | not started | no |
| 7. Docs and polish | `stage/7-docs` | not started | no |

## Latest summary

Stage 0 (bootstrap). No feature code.

- Done: scaffold verified and repaired (env-driven settings, `apps.wallets` created, app labels `apps.*`, missing subpackages, `startproject` leftovers removed, CI action versions bumped, stale `RISK_LOCK_DATABASE_URL` removed, `.idea/` ignored). `make check` green: ruff, mypy, 5 import contracts kept, pytest collects 0 tests. Private files ignored and untracked; no private content in tracked files. Rules load on Read for `monolith-side`, `persistence`, `ops`; the other three have no matching files yet. The format hook works.
- Versions: Python 3.12.11, Django 5.2.18, psycopg 3.3.6 (binary), Postgres 16.15, websockets 17.2 (defaults as in `gotchas.md`). Local uv 0.7.13; CI installs the latest uv.
- Design topics settled: see "Decided" in `design-topics.md`.
- Deviations from fixed decisions (agreed with the user): D6 no leader lease (provider session policy instead; A-15 added); D2 and invariant 2 reworded (only `TICKET` is at-least-once; final messages have no ack and are re-sent within the loss window, D11); A-04 one ticket type and one deadline; A-02 `ERROR` gains `ref_type`; D4 gains the error taxonomy and the poison-message risk.
- How to review: `git diff` of `docs/`, `CLAUDE.md`, `.claude/rules/`, `pyproject.toml` (one contract flag), `config/settings/`.

## Carry-over for the next stage

Open questions, deviations to remember, assumptions added (IDs), proposed gotchas not yet moved to `gotchas.md`.

For stage 1:
- Expand A-01..A-15 and B-01..B-04 into `docs/PROVIDER_QUESTIONS.md`. New or changed since the seed: A-02 (`ERROR.ref_type`), A-04 (one ticket type), A-15 (message scope). The `TICKET` payload is not specified anywhere yet: declare it as a new assumption (stake as a decimal string, currency, odds, selection, a pseudonymous customer reference, no ticket type).
- The codec must parse `ERROR.ref_type`; close classification covers `ALREADY_CONNECTED` (short capped backoff, about 2 s) and `DISPLACED` (long backoff), both at the HTTP upgrade and as a close code after the handshake.

For stage 2 and later:
- Ticket fields beyond the seed: `recommendation`, `last_error_code`, `last_error_ref_type`, `escalation_reason`, `last_sent_at` (`ticket-lifecycle.md`). The sweeper never cancels `CONFIRM_PENDING` (that bet is accepted locally); the lifecycle diagram's "any non-terminal state -> CANCEL_PENDING" is too broad and must be narrowed in the transition table.
- `Clock` appears in both `ports.py` and `runtime/clock.py` in `PROJECT_STRUCTURE.md`; per "Decided" there is no clock port: drop it from `ports.py` when stage 3 creates the file.
- `.env.example` and compose carry a static `PROVIDER_WS_TOKEN`; A-11 describes client credentials plus a short-lived token. Fine for the stage 4 static token provider stub; rename the variables (prefix `RISK_`) when the integration settings are written.
- `config/settings/base.py` raises a bare `KeyError` when `DJANGO_SECRET_KEY` or `DATABASE_URL` is missing; acceptable fail-fast, revisit with the startup probe in stage 5.

Proposed gotchas (not yet in `gotchas.md`, need approval):
- The edit hook runs `ruff check --fix`, which deletes an import that is not used yet. Add an import in the same edit as its first use.
- A `run_db` semaphore permit must be released when the worker thread finishes, not when the awaiting coroutine times out; otherwise more threads than permits are busy and the admission bound is false.
