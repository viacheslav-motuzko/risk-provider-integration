# Progress and handoff

Written by the agent at the end of every stage and read at the start of the next one (sessions do not share context). Keep it short and factual. Folded into the README or removed in stage 6.

## Status

| Stage | Branch | State | Merged into main |
|-------|--------|-------|------------------|
| 0. Bootstrap | `stage/0-bootstrap` | done; scaffold and stage 0 docs in one commit by the user | yes |
| 1. Provider contract | `stage/1-protocol` | done, incl. the gate revision | yes |
| 2. Domain and persistence | `stage/2-domain-persistence` | done, awaiting review (uncommitted) | no |
| 3. Monolith side | `stage/3-monolith` | not started | no |
| 4. Runtime core and failure scenarios | `stage/4-runtime` | not started | no |
| 5. Operability | `stage/5-ops` | not started | no |
| 6. Docs and polish | `stage/6-docs` | not started | no |

## Latest summary

Stage 2 (domain and persistence). Design choices at the gate: all four recommendations (recorded in `design-topics.md`, "Decided", stage 2).

- Done:
  - `domain/states.py`, `domain/machine.py`, `domain/errors.py` (stdlib only):
    - `RULES` maps each `Trigger` to its source states, its target and whether it needs the lease owner. Helpers: `next_state`, `send_trigger`, `trigger_for_error` (encodes the "ignored" list: decline errors, `UNKNOWN_TICKET` on a cancellation, errors without a ref).
    - `CANCEL_REASONS`, `ESCALATION_REASONS`, `ERROR_REFS`. `PermanentApplyError` for stage 3's adapter.
  - `models.py` and migration `0001_risk_ticket`: `RiskTicket` with typed snapshot columns, a plain `bet_id`, `created_at` with a DB default, and three check constraints (valid state, cancel reason present, escalation reason present). Four partial indexes:
    - `risk_ticket_outbox` (`created_at`, OUTBOX states) serves `claim_due`.
    - `risk_ticket_retransmit` (`retransmit_at`, `SENT`) serves the retransmit part of `claim_due`.
    - `risk_ticket_awaiting` (`deadline_at`, `PENDING_SEND`/`SENT`) serves `find_expired` and `claim_sent_for_resend`.
    - `risk_ticket_closed` (`last_sent_at`, closed final states) serves `claim_finals_for_resend`.
  - `persistence/tickets.py`:
    - `create_ticket` refuses to run outside a transaction. Three claim functions use one `_claim` (SKIP LOCKED + lease, expired lease claimable). A `PENDING_SEND` or `SENT` ticket past its deadline is never claimed for sending; the sweeper owns it.
    - `record_send` derives the trigger from the claimed state. `record_accept`, `record_decline`, `record_domain_rejection`, `cancel` (the deadline is re-checked on DB time inside the update), `escalate`, `find_expired`, `db_now`.
    - Every transition is one conditional `UPDATE` built from `RULES`, clears the lease and returns a bool.
  - `persistence/db.py`: `DbRunner` (`await run_db(fn, *args)`). Semaphore permit released by the executor future's done callback; context copied with `copy_context().run`; `close_old_connections` around `atomic` + `set_config(..., true)` for the three timeouts; await timeout raises `OutcomeUnknown`; the constructor refuses an await timeout not above the statement timeout.
  - `persistence/errors.py`: `classify_apply_failure` (decision 1).
  - Tests: 244 total, 182 new.
    - Table test: every trigger x state pair against a table copied independently from `ticket-lifecycle.md`. Plus invariants: money-moving transitions leave only pre-acceptance states; every cancel and escalation has a reason; only sends need the lease.
    - Persistence on Postgres: claim selection, lease exclusion, expired lease and lost send, retransmit once, a reply beating the relay's commit (five reply kinds), a refused confirm beating the commit (bet still accepted), a refused cancellation, duplicate and late recommendations, the deadline on DB time, reconnect re-send paging, loss-window re-send for all three final types, reasons carried from the row, the DB refusing a cancellation without a reason, `makemigrations --check`.
    - Threads: two claimers get disjoint rows without waiting; two appliers apply one recommendation exactly once (20 rounds).
    - `run_db`: transaction + local timeouts set, and gone after the transaction ends; statement timeout aborts and is transient; context variable reaches the worker; failure rolls back; timed-out call is `OutcomeUnknown`, its transaction still commits, and the next unit is not admitted until the thread ends; the classification of real Postgres errors.
  - `tools/mutation_check.py` (`make mutate`): sabotages each invariant in the production code, runs the suite, restores the file and prints a table. All 19 sabotages are caught (table under "How to review").
  - `make check` and `make mutate` green.
- Deviations:
  - `.claude/rules/domain-protocol.md` says transition functions receive `now`. None of them needs time: every time comparison (lease, deadline, retransmit point, loss window) runs in SQL on DB time (D12), so `next_state` takes no `now`.
  - New trigger `FINAL_RESENT` (self-loop on closed final states), so the final re-send goes through the table and the lease like every other send. `ticket-lifecycle.md` updated.
  - `domain` duplicates the reason and message-ref enums of `protocol` by value: the import contract forbids `domain -> protocol`. Stage 4 maps by value.
  - Persistence does not log misses: it returns a bool; the caller (stage 4) logs and counts with the message context.
  - `PROJECT_STRUCTURE.md` updated to the real function names and `persistence/errors.py`.
  - Found by the first `make mutate` run and fixed in this stage:
    - The admission test proved nothing: with `max_workers=1` the executor itself keeps the probe from starting. It now waits past the await timeout and checks that the next call is still waiting for admission (a call admitted too early would time out too).
    - No test proved that the timeouts are transaction-local; added `test_timeouts_end_with_the_transaction`.
    - The reconnect paging test spun until the tickets' real 30 s deadline under a paging bug, so its result depended on timing. Its loop is now bounded and checks each ticket is re-sent exactly once.
    - Django's `Now()` is `STATEMENT_TIMESTAMP()` on PostgreSQL, not the transaction-start `now()`. `db_now()` used `SELECT now()`, a second clock; it now uses `statement_timestamp()`. D12 and `gotchas.md` say "`now()` is the transaction start time"; that is true for SQL `now()` but not for what our queries use. Proposed wording below.
  - The `/stage` text invoked for this session said "commit in small steps"; the repository's `.claude/commands/stage.md` and `CLAUDE.md` say nothing is committed before review. I followed the repository.
- Assumptions added: none with an ID. Column precision is a guess for stage 3 to confirm: stake `(18, 2)`, odds `(12, 4)`; error codes are stored raw as text.
- Open questions:
  - Row 8: if a broken monolith invariant (`PermanentApplyError`) also breaks compensation (void, release), the separate compensation transaction fails too. Stage 3 decides what the adapter does then (likely: compensation is idempotent and tolerates an already released hold).
  - `DbRunner.shutdown()` blocks. The runtime must call it off the loop and before the loop closes; otherwise a late done callback hits a closed loop.
  - The `OR` claim query was not checked with `EXPLAIN`; each branch has a matching partial index, and the non-terminal set is small.
- Proposed gotchas (need approval):
  - Using a `StrEnum` member as a model default or in a `Q` of an index or constraint writes `State['X']` into the migration, which ties migrations to the enum. Pass `.value`.
  - Django's `Now()` on PostgreSQL is `STATEMENT_TIMESTAMP()`: it advances inside a transaction, also inside pytest-django's per-test transaction. Raw SQL `now()` is the transaction start. Use one clock: `Now()` in queries, `statement_timestamp()` in raw SQL. (Replaces the wrong entry proposed earlier, "`now()` is constant inside the test transaction".)
  - A test with a loop that ends on a DB condition must bound the loop, or a bug turns it into a spin that ends at some real deadline.
  - `ExpressionWrapper` is generic only in django-stubs: quote the annotation, or it fails at import.
- How to review:
  1. `domain/states.py`, `domain/machine.py` against the table in `ticket-lifecycle.md`, then `tests/test_domain_machine.py`.
  2. `models.py` and the migration (indexes and constraints).
  3. `persistence/tickets.py`, then `tests/test_persistence_tickets.py` and `tests/test_persistence_concurrency.py`.
  4. `persistence/db.py` and `persistence/errors.py`, then `tests/test_persistence_db.py`.
  5. `git status` for new files; `git diff` for the doc edits.

`make mutate` result (each row: the sabotaged invariant, and a test that turns red; full lists from `make mutate`):

| Sabotage | Caught by |
|---|---|
| send transition ignores the lease owner | `test_expired_lease_is_claimable_and_the_previous_owner_loses_the_send` |
| transition ignores the expected state | `test_duplicate_recommendation_is_ignored`, `test_duplicated_recommendation_applied_by_two_appliers_at_once_applies_exactly_once` (+2) |
| transition does not clear the lease | `test_reply_that_beats_the_relays_commit_is_applied_and_frees_the_row` (+3) |
| claim without SKIP LOCKED | `test_concurrent_claimers_get_disjoint_rows_without_waiting_for_each_other` |
| an expired lease is not claimable | `test_expired_lease_is_claimable_and_the_previous_owner_loses_the_send` |
| claims ignore the deadline | `test_a_ticket_past_its_deadline_is_not_sent_but_left_to_the_sweeper` |
| the retransmit point is not cleared by a re-send | `test_ticket_is_retransmitted_once_at_its_retransmit_point` |
| the deadline cancellation trusts its caller | `test_deadline_cancellation_is_decided_on_db_time_by_the_update_itself` |
| the reconnect re-send ignores `since` | `test_resend_after_reconnect_pages_through_every_sent_ticket_once` |
| the final re-send ignores the window start | `test_final_messages_sent_within_the_loss_window_are_resent_whatever_their_type` |
| `create_ticket` runs outside a transaction | `test_create_ticket_refuses_to_run_outside_the_bets_transaction` |
| ACCEPT does not accept `PENDING_SEND` (reply before the relay's commit) | `test_reply_that_beats_the_relays_commit_is_applied_and_frees_the_row`, the table test (+7) |
| `APPLY_FAILED` also from `CONFIRM_PENDING` (refunds a captured stake) | `test_money_moving_transitions_leave_only_pre_acceptance_states`, the table test |
| `UNKNOWN_TICKET` on a cancellation escalates | `test_provider_error_maps_to_a_trigger_or_is_ignored` |
| the `run_db` permit is released when the caller stops waiting | `test_timed_out_run_db_is_outcome_unknown_and_its_transaction_still_commits` |
| context variables are not copied into the worker | `test_context_variables_reach_the_worker_thread` |
| timeouts set for the session instead of `SET LOCAL` | `test_timeouts_end_with_the_transaction` |
| the await timeout may be below the statement timeout | `test_await_timeout_must_exceed_statement_timeout` |
| `OperationalError` is not transient | `test_statement_timeout_aborts_the_query_and_counts_as_transient` |

Proposed commits (after review):
1. `feat(domain): ticket states and transition table` — `apps/risk_integration/domain/states.py`, `apps/risk_integration/domain/machine.py`, `apps/risk_integration/domain/errors.py`, `apps/risk_integration/tests/test_domain_machine.py`
2. `feat(persistence): RiskTicket model with partial indexes and check constraints` — `apps/risk_integration/models.py`, `apps/risk_integration/migrations/0001_risk_ticket.py`
3. `feat(persistence): ticket claims and conditional transitions` — `apps/risk_integration/persistence/tickets.py`, `apps/risk_integration/tests/tickets_helpers.py`, `apps/risk_integration/tests/test_persistence_tickets.py`, `apps/risk_integration/tests/test_persistence_concurrency.py`
4. `feat(persistence): run_db unit of work and apply-failure classification` — `apps/risk_integration/persistence/db.py`, `apps/risk_integration/persistence/errors.py`, `apps/risk_integration/tests/test_persistence_db.py`
5. `chore(tools): targeted mutation check (make mutate)` — `tools/mutation_check.py`, `Makefile`
6. `docs: stage 2 decisions, module layout and mutation check in the process, handoff` — `CLAUDE.md`, `docs/kb/design-topics.md`, `docs/kb/ticket-lifecycle.md`, `docs/PROJECT_STRUCTURE.md`, `docs/kb/progress.md`

## Carry-over for the next stage

Open questions, deviations to remember, assumptions added (IDs), proposed gotchas not yet moved to `gotchas.md`.

For stage 3:
- Use cases compose a transition and the monolith effects in one `run_db` unit: the conditional update first, side effects only if it returned True. Rows 4-6 call `record_accept` / `record_decline` / `record_domain_rejection`; rows 8-10 call `cancel(trigger)` and compensate. Row 8 runs in a new unit after the failed one rolled back.
- `place_bet` calls `create_ticket(bet_id, snapshot, deadline)` inside its own `atomic`; the snapshot is the `TicketSnapshot` dataclass. Match the column precision (stake `(18, 2)`, odds `(12, 4)`) or change it there.
- `Clock` appears in both `ports.py` and `runtime/clock.py` in `PROJECT_STRUCTURE.md`. Per "Decided" there is no clock port: drop it from `ports.py` when stage 3 creates the file.
- Decide the adapter's behavior when compensation meets the same broken invariant as the failed apply (open question above).

For stage 4:
- Applier: catch the exception of a failed apply, `classify_apply_failure`; `None` re-raises (fail fast), `TRANSIENT` leaves the ticket, `PERMANENT` runs the row 8 unit. Log and count every transition that returns False.
- Relay: `claim_due` on every poll; after a (re)connect, take `since = db_now()` and page `claim_sent_for_resend` and `claim_finals_for_resend` until empty; after each send, `record_send(ticket_id, claimed.state, ...)`. Map `CancelReason`/`MessageRef` to protocol enums by value.
- Open question for the gate: D6 makes `AUTH_FAILED` fatal (the process exits). An auth outage on the provider side would then crash-loop every pod, which conflicts with the spirit of invariant 7. Alternative: long backoff, a session-status metric and an alert, with no exit. Recommendation: long backoff plus alert.
- The reader catches `FrameError` and counts `kind` as the metric label. It logs `message_type` and `ticket_id` from the error.
- `encode` needs an aware `ts`: pass wall-clock UTC. It is informational only (D12).
- Open point for the design note: the loss-window anchor (`window_start` of `claim_finals_for_resend`) is the disconnect time, which a freshly started process does not know. Candidate: anchor on `max(last_sent_at)` of closed tickets at the first connect of a process.
- The fake provider needs the opposite direction (decode outbound, encode inbound). Either it builds JSON from the same `Envelope` model, or the codec gains two test-facing helpers. Do not put fake-only code in `protocol/` without saying so.
- `.env.example` and compose carry a static `PROVIDER_WS_TOKEN`; rename to the `RISK_` prefix when the integration settings are written. The settings also size `DbRunner` (workers = applier count, D4) and its timeouts.
- Call `DbRunner.shutdown()` off the loop, before the loop closes.
- For the design note: `run_db` bounds only the work, not the wait for admission. A caller can wait for a permit without limit, which breaks "every DB operation has an explicit timeout" (invariant 5, async-runtime rule). Proposal: bound the admission wait and raise a separate error for it (for example `NotAdmitted`). Its outcome is known (the work never ran), so it is transient and needs no re-read, unlike `OutcomeUnknown`. Also, no `connect_timeout` is set in the database options, so connecting to an unreachable DB can hang for the TCP timeout. Set it with the integration settings. Both found in stage 2 by the mutation review.
- `config/settings/base.py` raises a bare `KeyError` when `DJANGO_SECRET_KEY` or `DATABASE_URL` is missing. Acceptable fail-fast; revisit with the startup probe in stage 5.

Proposed gotchas from stage 1 (not yet in `gotchas.md`, need approval):
- The edit hook runs `ruff check --fix`, which deletes an import that is not used yet. Add an import in the same edit as its first use.
- A `run_db` semaphore permit must be released when the worker thread finishes, not when the awaiting coroutine times out (now implemented and tested in stage 2; the entry would document why).
