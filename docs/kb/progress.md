# Progress and handoff

Written by the agent at the end of every stage and read at the start of the next one (sessions do not share context). Keep it short and factual. Folded into the README or removed in stage 6.

## Status

| Stage | Branch | State | Merged into main |
|-------|--------|-------|------------------|
| 0. Bootstrap | `stage/0-bootstrap` | done; scaffold and stage 0 docs in one commit by the user | yes |
| 1. Provider contract | `stage/1-protocol` | done, incl. the gate revision | yes |
| 2. Domain and persistence | `stage/2-domain-persistence` | done, reviewed, committed | yes |
| 3. Monolith side | `stage/3-monolith` | done, reviewed, committed | yes |
| 4a. Runtime loops and scenarios | `stage/4a-runtime` | done, awaiting review (uncommitted) | no |
| 4b. Session resilience | `stage/4b-session` | not started | no |
| 5. Operability | `stage/5-ops` | not started | no |
| 6. Docs and polish | `stage/6-docs` | not started | no |

## Latest summary

Stage 4a (runtime loops and scenarios). Gate decisions (recorded in `design-topics.md`, "Decided", stage 4 gate): stage 4 split into 4a and 4b; `AUTH_FAILED` gets a long backoff and an alert instead of an exit (implemented in 4b); the relay lives inside each session; the loss window for final re-sends ends at the latest final send.

- Done:
  - `transport/ws_client.py`:
    - `open_session` yields a `ProviderSession` with `send` (with a timeout), `recv` and `closed`, a watch that raises even while nobody reads.
    - The library's exceptions become `SessionEnded(outcome)`, classified by `protocol.close_codes`.
    - `StubTokenProvider`.
    - The connection gets its own logger pinned at INFO: websockets logs request headers, the bearer token among them, at DEBUG.
  - `runtime/`:
    - `connection.py`: one session group (reader, relay, socket watch). A `SessionEnded` raised alone ends the session and, for now, the process; next to a bug, the bug ends the process.
    - `relay.py`: each session starts with `since = db_now()`, re-sends every `SENT` ticket, then every final message in `[latest final send − loss window, since)`, then polls `claim_due`. Send, then `record_send`. A transient DB error after the send lets the lease run out (the next claim sends again). It takes a narrow `Sender` protocol.
    - `inbound.py`:
      - The reader decodes; a `FrameError` is dropped and counted by `kind`; everything else goes into the bounded queue.
      - Appliers route `RECOMMENDATION` to apply. An `ERROR` goes through `trigger_for_error`: a refused `TICKET` is compensated, a refused confirm or cancellation escalated, uncorrelated and no-effect errors are ignored and counted.
    - `decisions.py`: `apply`, `compensate` and `escalate`, each one `run_db` unit, with the failure classes from the stage 3 handoff. A lock-timeout gets its own label. `escalate` is retried in place (3 attempts, 0.5 s apart) on transient DB errors; `compensate` reports whether it failed.
    - `sweeper.py`: `find_expired`, then compensate with `DEADLINE_PASSED`; it always pauses between passes. A ticket whose compensation failed is left out of its batches for `RISK_SWEEPER_RETRY_FAILED_AFTER` (5 s).
    - `supervisor.py`: `supervise`; a loop that returns raises `LoopReturned`.
    - `config.py`: `IntegrationSettings` (`RISK_*`, durations in seconds).
      - `wss://` unless `RISK_PROVIDER_ALLOW_PLAINTEXT` is set.
      - The lease must exceed the send timeout plus the await timeout, and the await timeout must exceed the statement timeout. Defaults: lease 6 s, send timeout 2 s.
      - `loss_window` is the ping interval plus the ping timeout; `db_workers` is appliers + 2.
    - `metrics.py`: 13 counters with bounded labels, served in stage 5.
    - `app.py`: composition root; the lease owner is unique per process; the stubs are logged at startup; `DbRunner.shutdown()` runs off the loop.
  - `management/commands/run_risk_provider.py`: invalid settings raise a `CommandError` that names the variables and never echoes values.
  - `persistence`:
    - `NotAdmitted`: the wait for a `run_db` permit is now bounded.
    - `TRANSIENT_DB_ERRORS` and `transient_kind`, giving the labels `lock_timeout`, `statement_timeout`, `outcome_unknown`, `not_admitted` and `db_error`.
    - `last_final_sent_at`; `find_expired` takes `exclude`.
  - `config/settings/base.py`: `DATABASE_CONN_MAX_AGE` (default 0, set per Deployment; the command warns at startup when it is 0), `CONN_HEALTH_CHECKS`, `DATABASE_CONNECT_TIMEOUT` (default 2 s, below the await timeout; a value in `DATABASE_URL` wins).
  - `tools/fake_provider/server.py`. Knobs: `ticket_reply` (accept, decline, silent, refuse), `duplicates`, `refuse_confirm`, `refuse_cancellation`, `send`, `close_sessions`. It checks the bearer token.
  - Tests: 325 total, 39 new.
    - Scenarios (`tests/test_runtime_scenarios.py`, real loops, real Postgres, the fake over a real socket):
      - accept, decline, and a decline by our limits with its reason on the wire;
      - a duplicate recommendation moves money once;
      - a crash between apply and confirm;
      - a restart with `SENT` tickets, resolved by re-sending at once;
      - final messages in the loss window re-sent at the first connect, an older one not;
      - silence: one retransmission, then compensation and cancellation at the deadline;
      - a late recommendation after the cancellation is ignored;
      - a refused ticket is compensated;
      - a refused confirm escalates with the bet still accepted;
      - a refused cancellation escalates with the bet still voided;
      - bad frames, unknown tickets and uncorrelated errors are dropped and counted;
      - each apply failure class: a transient one is healed by the retransmission, a permanent one compensates, an unclassified one ends the process and moves no money;
      - a provider close and a refused token end the process with their outcome;
      - an escalation that fails transiently is retried and escalates; one that keeps failing gives up after its attempts and is counted;
      - a ticket whose compensation keeps failing does not hold back the next expired one.
    - Units (`test_runtime_units.py`):
      - the supervisor: a returning loop is a failure, a failing loop ends the group;
      - persisted and wire enums map one to one;
      - a failed send is not recorded;
      - settings validation;
      - the command does not echo values, and warns at startup when DB connections are not persistent.
    - Persistence: bounded admission; the lock-timeout label; the loss-window anchor ignores open tickets.
  - `make check` green. `make mutate`: 43 sabotages, 15 new, all caught (table below; `operational-unclassified` repointed, see Deviations).
- Deviations:
  - Branch `stage/4a-runtime` (the table said `stage/4-runtime`; renamed for the split).
  - The diff is over budget even after the split: 671 non-blank lines in new modules (imports and docstrings included) plus about 50 changed lines, against about 600.
  - Invariant 4 in `CLAUDE.md` reworded at the review (it said "One `TaskGroup`"): one top-level group, each session's loops in a nested group that ends with its session. The failure semantics are the same: a bug anywhere ends the process.
  - D4 and D5 updated: the executor has appliers + 2 workers instead of exactly N (the relay and the sweeper cannot be starved by a burst of applies); bounded admission (`NotAdmitted`); connection reuse per Deployment.
  - D11 updated: the loss-window end is the latest final send. The monotonic in-process anchor from the design note is not needed: the relay sends nothing between sessions, so one rule covers a restart and an in-process reconnect.
  - Not created: `runtime/clock.py` (nothing needs an async clock seam: loops use `asyncio.sleep`, tests move time in the DB), `tools/fake_provider/scenarios.py` (the knobs are attributes) and `__main__.py` (stage 5, for compose). `tools/__init__.py` added. `PROJECT_STRUCTURE.md` updated.
  - `pydantic.mypy` plugin enabled: without it mypy treats `BaseSettings` fields as required arguments. It ships with pydantic, so no new dependency. A stage 1 test that writes to a frozen model on purpose now needs `# type: ignore[misc]`.
  - Heartbeats are not built: liveness is stage 5. Every loop iteration has an obvious bump point.
  - Stage 2's `operational-unclassified` mutation entry pointed at the old union in `errors.py`; I repointed it at the new tuple. Nothing else in the earlier work changed. This stage's `deadline-ignored` was repointed after the sweeper change in review round 2.
  - `.env.example` and compose: `PROVIDER_WS_*` renamed to `RISK_PROVIDER_URL`, `RISK_PROVIDER_TOKEN`, plus `RISK_PROVIDER_ALLOW_PLAINTEXT`. Your local `.env` still has the old names: `make run` needs the new ones.
  - No new dependencies, no migration.
- Assumptions added: none with an ID. The bearer token in the `Authorization` header of the upgrade request is now marked `# ASSUMPTION:` in the transport (it is part of A-11).
- Open questions for the gate (all four decided at the review, round 2; kept for the record):
  1. **A lost escalation.** If the `escalate` unit for a refused confirm or cancellation fails transiently, the refusal is lost: the provider does not repeat an error. Only a later re-send within the loss window could draw it again. Today it is logged at error level and counted (`risk_escalation_failures_total`). Options:
     - (a) accept it, with an alert (current);
     - (b) a bounded inline retry (a few attempts over about 2 s) for error-driven transitions only, an exception to D4's no-inline-retry: D4's reason (retransmission recovers it) does not hold for errors;
     - (c) an inbox table. It does not help during a real DB outage, because the insert fails too.

     Recommendation: (b).
  2. **Sweeper starvation.** `find_expired` orders by deadline. With at least `sweeper_batch_size` (100) tickets whose compensation keeps failing (accepted bets behind waiting tickets, each already alerted), newer expired tickets are never reached. Options: accept it as an alerting case; or skip, in memory, tickets that failed in the last N minutes. Recommendation: the in-memory skip, in 4b.
  3. **Lease versus deadline after a crash.** A batch claimed by a dead process waits out its lease (default 10 s, which must exceed send timeout 5 s + await timeout 3 s), so `PENDING_SEND` tickets in it can reach the 10 s deadline and be cancelled instead of sent. 4b releases the process's own leases at session start, which helps only in-process reconnects (the owner is unique per process). Option: lower the defaults (send timeout 2 s, lease 6 s). Recommendation: lower the defaults in 4b.
  4. Invariant 4 wording (see Deviations).
- Proposed gotchas:
  - `make` exports `.env` into every target, tests included: a test that reads the environment must clear what it does not set itself.
  - websockets logs the handshake request headers at DEBUG, `Authorization` included. Give the connection its own logger pinned at INFO.
  - pydantic v2 reads a numeric string for a `timedelta` as ISO 8601 and rejects `"0.25"`. Environment values need a seconds `BeforeValidator`.
  - mypy needs the `pydantic.mypy` plugin to know that `BaseSettings` fields may come from the environment.
  - Raising one exception inside `except*` replaces the group if nothing else is left unhandled; anything still unhandled is grouped with it. That unwraps an expected exception without hiding a bug.
  - In tests, a bounded DB poll trips ruff `ASYNC110`. There is no event to wait on: `noqa` with the reason.
- Review fixes (round 1, after the first summary):
  - `DATABASE_CONNECT_TIMEOUT` defaults to 2 s instead of 5 s: below `RISK_DB_AWAIT_TIMEOUT` (3 s), so an unreachable database surfaces as a transient `OperationalError` instead of `OutcomeUnknown`; 2 s is the minimum psycopg honours (without a value it waits 130 s).
  - `connect_timeout` is set only when `DATABASE_URL` does not carry one; before, the URL value was silently overwritten.
  - The command logs `db_connections_not_persistent` at startup when `CONN_MAX_AGE` is 0, so a Deployment that forgot `DATABASE_CONN_MAX_AGE` is visible. The default stays 0: the settings module is shared with the web pods. Unit test for both values.
  - D5: the reasons for the default, the health checks and the connect timeout; why Django's native pool is not used.
  - Considered and kept as they are (discussed at the review): applying in the reader without a queue and appliers, one loop for relay and sweeper, the websockets reconnect iterator for 4b (fixed per-attempt headers, one delay curve for all outcomes, backoff reset after a handshake).
- Review fixes (round 2, the four open questions, decided as recommended):
  - Escalation retry: `Decisions.escalate` retries `tickets.escalate` in place, 3 attempts 0.5 s apart, on transient DB errors only, then gives up with `escalation_failed` and `risk_escalation_failures_total`. The one exception to no inline retry, recorded in D4, `design-topics.md` and lifecycle rows 14-15.
  - Sweeper starvation: a ticket whose compensation failed is skipped in memory for 5 s (`RISK_SWEEPER_RETRY_FAILED_AFTER`). D4.
  - Defaults: lease 10 s to 6 s, send timeout 5 s to 2 s. D3.
  - Invariant 4 reworded in `CLAUDE.md`; D4 says "one top-level `TaskGroup`".
  - Two new mutation entries (`escalation-not-retried`, `failed-compensation-starves`); `deadline-ignored` repointed. `make mutate`: 43 sabotages, all caught.
  - Found while rerunning `make check`: `make` exports `.env`, so a local `RISK_PROVIDER_ALLOW_PLAINTEXT=1` made `test_plain_ws_needs_an_explicit_opt_in` pass or fail depending on the developer's machine (plain `pytest` passed, `make test` failed). A root `conftest.py` removes every `RISK_*` variable for each test.
- How to review:
  1. `runtime/connection.py`, `relay.py`, `inbound.py`, `decisions.py`, `sweeper.py`, `supervisor.py`: the loops, where state is saved, the failure classes.
  2. `transport/ws_client.py`: outcomes, timeouts, the token.
  3. `runtime/config.py`, `runtime/app.py`, `management/commands/run_risk_provider.py`: defaults, validation, wiring.
  4. `persistence/db.py` (`NotAdmitted`), `persistence/errors.py`, `persistence/tickets.py` (`last_final_sent_at`), `config/settings/base.py`.
  5. `tests/test_runtime_scenarios.py`, `tests/scenario_helpers.py`, `tools/fake_provider/server.py`, `apps/risk_integration/tests/test_runtime_units.py`.
  6. `git diff` for `decisions.md` (D4, D5, D11), `design-topics.md`, `stages.md`, `PROJECT_STRUCTURE.md`, `.env.example`, `docker-compose.yml`, `pyproject.toml`, `tools/mutation_check.py`.

`make mutate` result for the entries added in this stage (the 28 earlier entries are still caught; full table from `make mutate`):

| Sabotage | Caught by |
|---|---|
| `operational-unclassified`: OperationalError is not transient | caught: `TestRunDb::test_statement_timeout_aborts_the_query_and_counts_as_transient`, `test_a_failed_transition_rolls_back_the_monolith_effect`, `test_a_transient_apply_failure_leaves_the_ticket_for_the_retransmission` |
| `admission-unbounded`: the wait for a run_db permit is not bounded | caught: `TestRunDb::test_the_wait_for_admission_is_bounded_and_the_refused_work_never_runs` |
| `not-admitted-unclassified`: a refused admission is not transient (fails the process) | caught: `TestApplyFailureClassification::test_unknown_outcome_and_refused_admission_are_transient` |
| `lock-timeout-unlabelled`: a wait on the ticket lock past lock_timeout reads as a database error | caught: `TestRunDb::test_a_wait_on_the_ticket_lock_past_lock_timeout_has_its_own_label` |
| `resend-skipped`: a new session does not re-send what the previous one may have lost | caught: `test_after_a_restart_sent_tickets_are_retransmitted_at_once_and_resolved`, `test_final_messages_sent_in_the_loss_window_are_resent_at_the_first_connect` |
| `loss-window-from-connect`: the final re-send window ends at the connect, not at the last final send | caught: `test_final_messages_sent_in_the_loss_window_are_resent_at_the_first_connect` |
| `commit-before-send`: the send is committed without waiting for the frame to be written | caught: `test_a_send_that_fails_is_not_recorded_so_the_message_is_still_owed` |
| `transient-compensates`: a transient apply failure compensates and cancels instead of waiting for the retransmission | caught: `test_a_transient_apply_failure_leaves_the_ticket_for_the_retransmission` |
| `unclassified-swallowed`: an unclassified apply failure is swallowed instead of ending the process | caught: `test_an_unclassified_apply_failure_ends_the_process_and_moves_no_money` |
| `permanent-not-compensated`: a permanent apply failure leaves the stake held instead of compensating | caught: `test_a_permanent_apply_failure_compensates_and_cancels` |
| `refused-ticket-escalates`: a refused TICKET is escalated instead of compensated | caught: `test_a_refused_ticket_is_compensated_and_cancelled` |
| `refusal-ignored`: a refused confirm or cancellation is not escalated | caught: `test_a_refused_cancellation_escalates_with_the_bet_still_voided`, `test_a_refused_confirm_escalates_with_the_bet_still_accepted` |
| `returning-loop-ok`: a loop that returns is not treated as a failure | caught: `TestSupervisor::test_a_loop_that_returns_is_a_failure_and_cancels_the_others` |
| `deadline-ignored`: the sweeper finds expired tickets but does not cancel them | caught: `test_a_refused_cancellation_escalates_with_the_bet_still_voided`, `test_a_ticket_whose_compensation_keeps_failing_does_not_hold_back_the_next_expired_one`, `test_provider_silence_retransmits_once_then_compensates_and_cancels_at_the_deadline` |
| `failed-compensation-starves`: the sweeper keeps picking tickets whose compensation just failed, ahead of the ones behind them | caught: `test_a_ticket_whose_compensation_keeps_failing_does_not_hold_back_the_next_expired_one` |
| `escalation-not-retried`: a transient failure of an escalation gives up at once | caught: `test_a_refusal_whose_escalation_fails_transiently_is_retried_and_escalates`, `test_an_escalation_that_keeps_failing_gives_up_after_its_attempts_and_is_counted` |

Proposed commit (after review): `stage 4a: Runtime loops and scenarios`, the whole working tree of the stage.

## Carry-over for the next stage

Open questions, deviations to remember, assumptions added (IDs), proposed gotchas not yet moved to `gotchas.md`.

For stage 4b:
- The reconnect loop goes into `runtime/connection.py`, around the session group, catching `SessionEnded` and using `ended.outcome` for the backoff. `PLANNED` reconnects at once; `ALREADY_CONNECTED` gets a short capped backoff with jitter; `DISPLACED` a long one; `AUTH_FAILED` a long one with an alert (gate decision; reword D6). The attempt counter resets after a session that stayed up for a while, not after a handshake. Unit test of the backoff per outcome. Rewrite the two 4a tests that expect the process to end on a closed session or a refused token.
- Fetch the token with a timeout (`tokens.token()` is awaited without one; the stub cannot hang, a real provider can).
- At session start, release the leases this process holds (a batch claimed when the old socket died); the lower lease default covers a crash, this covers an in-process reconnect.
- The `session_ended` log line lacks `connection_id` (logged outside the bound context).
- Fake provider: a reject-new knob (409 at the upgrade, or close 4003 after the handshake). Scenario: a second instance is refused and takes over after the first closes; a reconnect re-sends `SENT` tickets and final messages.
- Label a disconnect caused by a full inbound queue (the reader blocked, pongs unread) separately in the session-ended metric.

For stage 5:
- Serve `runtime/metrics.py` and add the gauges from the ops rule (time to decision, oldest unresolved ticket, tickets by state, outbox depth, queue depth, session status). Heartbeats: one bump per loop iteration in the relay, appliers, sweeper and connection.
- structlog JSON rendering, UTC timestamps; keep the websockets logger at INFO or above.
- Drain on SIGTERM: the relay stops claiming `PENDING_SEND`, the reader stops, the appliers finish the queue (add `task_done`/`join`), `*_PENDING` is flushed once, the session closes with 1001 (today `ws.close()` sends 1000).
- The integration Deployment sets `DATABASE_CONN_MAX_AGE` (for example 60); web pods keep 0.
- `tools/fake_provider/__main__.py` for compose.
- The monolith's services run under our `SET LOCAL` `statement_timeout` (2 s) and `lock_timeout` (1 s); size them against the monolith's real operations.
- `config/settings/base.py` raises a bare `KeyError` when `DJANGO_SECRET_KEY` or `DATABASE_URL` is missing; revisit with the startup probe.
