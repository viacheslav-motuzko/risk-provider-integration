# Progress and handoff

Written by the agent at the end of every stage and read at the start of the next one (sessions do not share context). Keep it short and factual. Folded into the README or removed in stage 6.

## Status

| Stage | Branch | State | Merged into main |
|-------|--------|-------|------------------|
| 0. Bootstrap | `stage/0-bootstrap` | done; scaffold and stage 0 docs in one commit by the user | yes |
| 1. Provider contract | `stage/1-protocol` | done, incl. the gate revision | yes |
| 2. Domain and persistence | `stage/2-domain-persistence` | done, reviewed, committed | yes |
| 3. Monolith side | `stage/3-monolith` | done, reviewed, committed | yes |
| 4a. Runtime loops and scenarios | `stage/4a-runtime` | done, reviewed, committed | yes |
| 4b. Session resilience | `stage/4b-session` | done, reviewed, committed | yes |
| 5. Operability | `stage/5-ops` | done, awaiting review (uncommitted) | no |
| 6. Docs and polish | `stage/6-docs` | not started | no |

## Latest summary

Stage 5 (operability). Gate decisions at the design note, all as recommended: liveness by a deadline per beat with `park()`; readiness "DB reachable" passive (last committed unit of work); the drain runs in the connection task; the probe server is a context manager, not a supervised task. Recorded in D4, D8 and `design-topics.md`.

- Done:
  - `runtime/health.py` (new): `Heartbeats`. Each beat sets a deadline: now + `RISK_LIVENESS_STALE_AFTER` (60 s) + the loop's planned wait. `park()` covers an empty queue, or an open session whose relay beats. `Health` holds the startup, liveness and readiness checks. `serve_health` is an aiohttp server on `RISK_HEALTH_PORT` (9100): `/startupz`, `/livez` (body: state per loop), `/readyz`, `/metrics`.
  - Startup (`runtime/app.py`): the probe server comes up first. The app then checks `persistence/schema.py: pending_migrations` through `run_db` every `RISK_STARTUP_RETRY` (2 s), and no loop starts before the DB is reachable and nothing is pending.
  - Readiness: started, not draining, and a unit of work committed within `RISK_READINESS_DB_WITHIN` (10 s). `DbRunner.last_success` is new.
  - Drain on SIGTERM:
    - `runtime/shutdown.py` (new): `Shutdown`, `StopRequested`, `Drained`.
    - The session group gets a stop task; ending the group cancels the reader and the relay.
    - `connection._drain`: `queue.join()`, then `Relay.flush` (one pass over `claim_final_pending`: `CONFIRM/DECLINE/CANCEL_PENDING`, never `PENDING_SEND` or `SENT`), then `ProviderSession.close(1001)`. The join and the flush share `RISK_DRAIN_TIMEOUT` (10 s).
    - `app.run` catches `Drained` and returns, so the process exits 0. Without a session, a shutdown exits at once, also from inside a reconnect delay. Appliers call `task_done`.
  - Signal handling (`run_risk_provider`): the event loop's SIGTERM/SIGINT handlers call `shutdown.request`. They are installed before the app's slow start (waiting for the database).
  - Logging (`runtime/logs.py`, new): structlog JSON on stdout with UTC ISO timestamps; stdlib loggers go through the same renderer; `RISK_LOG_LEVEL`; no access log for probes.
  - Metrics:
    - `risk_time_to_decision_seconds{decision}`: the ticket's age at apply, on DB time. `lock_awaiting` returns it, and `apply_recommendation` returns `Applied(state, waited)`.
    - `risk_inbound_queue_depth`.
    - Both are in-process. Database-wide backlog gauges were built and then removed in review (see Deviations).
  - `config/settings/base.py`: a missing `DJANGO_SECRET_KEY` or `DATABASE_URL` raises `ImproperlyConfigured` with the variable's name, instead of a bare `KeyError`.
  - `RISK_WS_CLOSE_TIMEOUT` default 10 s -> 2 s (4b open question 2).
  - Dockerfile: multi-stage, `python:3.12.11-slim-bookworm`, uv 0.7.13, non-root uid 10001, exec-form `CMD`. Target `app` (default) has no tests or tools; target `fake-provider` adds `tools/`.
  - `docker-compose.yml`: `db`, a one-shot `migrate`, `fake-provider`, and `app` (healthcheck on `/readyz`, `stop_grace_period: 30s`). `make up` and `make demo` are new.
  - `tools/fake_provider`: `host`/`port`, recorded client close codes (`close_codes`, `wait_for_close`), `__main__.py` for compose.
  - Tests: 369 in total, 20 more than after 4b.
    - Scenarios (`tests/test_runtime_shutdown.py`):
      - the drain lets the appliers finish a queue held mid-apply, sends their confirms once, does not send a bet placed after the drain started, reports not ready while draining, closes with 1001 and exits cleanly;
      - a drain that runs out of time still closes with 1001, and a successor sends the confirm that was left;
      - a shutdown without a session does not wait out a 60 s backoff;
      - a refused standby is ready and live;
      - no loop runs and the probes stay red while a migration is pending.
    - One process test (`tests/test_command_process.py`): the real command, a real SIGTERM, exit 0, close 1001, every stdout line JSON.
    - Units: heartbeats (deadline, planned wait, park); relay and sweeper keep beating while the DB is down; readiness; the probes over HTTP; pending migrations; flush (only final messages, pages, releases on a dead session); the claim for the flush; time to decision on DB time.
  - Checked by hand: `docker compose --profile app up --build --wait`, `make demo` (5 bets confirmed, JSON logs, probes, metrics). `docker compose stop app` sends SIGTERM to Python as PID 1: drained, exit 0, under 1 s. The image also runs with `--read-only` and a tmpfs `/tmp`.
  - `make check` green. `make mutate`: 72 sabotages, 16 new, all caught (table below).
- Deviations:
  - **Return types from stages 2 and 3 changed:** `tickets.lock_awaiting` returns `AwaitingTicket(bet_id, age)` instead of the bet id, and `services.apply_recommendation` returns `Applied(state, waited)` instead of the state, so the time to decision is measured on DB time in the same unit of work. Tests that compared the state use `flow_helpers.applied_state`.
  - D4's task list changed: the probe server is a context manager (gate decision).
  - **Backlog gauges removed in review.** I had built `risk_tickets{state}` and `risk_oldest_unresolved_ticket_age_seconds`, read every 15 s by a fifth loop. They were removed as too much system for the assignment: an extra loop, and database-wide numbers that every pod reports, for failures the session, error and escalation metrics already signal. The production path recorded in D8 is a SQL exporter. **`.claude/rules/ops.md` was amended**: its metric list named these gauges.
  - A plain SIGTERM handler at the top of the command was removed in review: it covered only milliseconds before the loop's handler, while Django's setup before the command stays uncovered anyway (D8).
  - **No Kubernetes manifest.** I wrote `deploy/k8s/deployment.yaml`, trimmed it, then deleted it in review: the task asks for a Kubernetes-friendly process, not manifests. The wiring (probe periods and thresholds, grace period, rollout strategy, resources) is recorded in D8 for the README. `stages.md` and `.claude/rules/ops.md` were amended, since both named the manifest.
  - After `stale()` was removed, `Heartbeats.report()` is the single read, with one clock reading, so a probe's status and body always agree (review).
  - **One cross-process test**, despite "no cross-process e2e suite" (stage 1 gate). It is the only way to test the signal handler and the JSON rendering together; it takes about 1 s.
  - The status table said 4b was uncommitted, although it was merged; corrected.
  - The 4b sabotage `leases-kept-at-session-end` now also removes the flush's release (same line, replaced everywhere); still caught.
  - The 4b sabotage `backpressure-unlabelled` matches `c.queue.full()` now: the connection task's parameters became one `Connection` value. The first full run stopped on the stale pattern; no file was left sabotaged.
  - Size: about 600 lines of non-test Python, within the budget, plus about 200 lines of Dockerfile, compose and YAML.
  - No new dependency: aiohttp, prometheus-client and structlog were already pinned. No migration. `.env.example` unchanged: the new variables have defaults.
- Assumptions added: none with an ID.
- Open questions for the gate:
  1. **Readiness never requires the session** by construction (`Health` gets nothing about the session), but no sabotage can prove it: `risk_session_open` is a process-wide gauge, so in a two-instance test it shows the holder's session to the standby too. The standby test proves the behavior today.
  2. **Resource numbers in D8** (100m/256Mi requested, 512Mi memory limit) are not measured. The demo process uses roughly 100 MB.
  3. **The sweeper runs during the drain.** Its cancellations are durable, and they go out on the next session if the flush has already passed them.
  4. **SIGTERM during Django's setup and settings validation**, before the loop's handler exists, is still ignored when Python is PID 1. Nothing runs yet, so the kubelet kills the pod at the grace period. An init process (tini) would close that window; I did not think it was worth the extra binary.
  5. Image tags are pinned by version, not by digest.
- Gotchas: the five from this stage and the four from 4b were moved to `gotchas.md` after review. One more was added there, found in this stage: Prometheus metrics are process-wide, so a two-instance test cannot assert one instance's gauge.
- How to review:
  1. `runtime/health.py` (heartbeats, checks, server), then the beats in `relay.py`, `sweeper.py`, `inbound.py`, `connection.py`.
  2. `runtime/connection.py` (`_run_session` stop task, `_drain`), `runtime/shutdown.py`, `runtime/relay.py: flush`, `persistence/tickets.py: claim_final_pending`.
  3. `runtime/app.py` (startup gate, `except* Drained`), `management/commands/run_risk_provider.py` (signal handlers), `persistence/schema.py`, `persistence/db.py: last_success`.
  4. `runtime/metrics.py`, `persistence/tickets.py: lock_awaiting`, `services.py: Applied`, `runtime/decisions.py`.
  5. `runtime/config.py`, `runtime/logs.py`, `config/settings/base.py`.
  6. `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `Makefile`, `tools/fake_provider/`.
  7. Tests: `tests/test_runtime_shutdown.py`, `tests/test_command_process.py`, `apps/risk_integration/tests/test_runtime_health.py`, `TestFlush` in `test_runtime_units.py`, the new tests in `test_persistence_tickets.py` and `test_bet_flow.py`.
  8. `git diff` of `decisions.md` (D4, D8), `design-topics.md`, `PROJECT_STRUCTURE.md`, `tools/mutation_check.py`.

`make mutate` result for the entries added in this stage (final full run after the review changes: all 72 caught, the 56 earlier entries included):

| Sabotage | Caught by |
|---|---|
| `liveness-blind`: liveness never reports a stale loop | caught: `TestHeartbeats::test_a_loop_that_beat_within_the_limit_is_on_time_and_one_that_did_not_is_stale`, `TestHeartbeats::test_a_parked_loop_is_never_stale_until_it_beats_again`, `TestHeartbeats::test_a_planned_wait_moves_the_deadline_by_its_length`, `test_the_probes_answer_over_http_with_their_status_and_the_metrics_are_served` |
| `sweeper-silent-on-db-error`: the sweeper beats only after handling a ticket, so a database outage reads as a hung loop | caught: `test_a_loop_keeps_beating_while_the_database_is_down_so_liveness_stays_green[...]` |
| `relay-silent-on-db-error`: the relay beats only after a send, so a database outage reads as a hung loop | caught: `test_a_loop_keeps_beating_while_the_database_is_down_so_liveness_stays_green[...]` |
| `ready-while-draining`: a draining pod still reports ready | caught: `TestReadiness::test_a_draining_pod_is_not_ready_because_it_can_no_longer_take_over`, `test_a_shutdown_lets_the_appliers_finish_sends_their_decisions_once_and_closes_with_going_away` |
| `ready-without-db`: readiness ignores whether the database was reached lately | caught: `TestReadiness::test_a_pod_that_has_not_reached_the_database_lately_is_not_ready` |
| `db-success-not-recorded`: the runner never records a committed unit of work, so no pod is ever ready | caught: `test_a_refused_standby_is_ready_and_live_because_it_can_take_over`, `test_sigterm_drains_closes_with_going_away_and_exits_zero_with_json_logs` |
| `start-with-pending-migrations`: the loops start while migrations are pending | caught: `test_no_loop_runs_until_every_migration_is_applied` |
| `session-ignores-shutdown`: a shutdown request does not stop the session, so the reader and the relay go on | caught: `test_a_drain_that_runs_out_of_time_still_closes_and_a_successor_sends_what_was_left`, `test_a_shutdown_lets_the_appliers_finish_sends_their_decisions_once_and_closes_with_going_away`, `test_sigterm_drains_closes_with_going_away_and_exits_zero_with_json_logs` |
| `drain-skips-queue`: the drain does not wait for the appliers to finish what was read | caught: `test_a_drain_that_runs_out_of_time_still_closes_and_a_successor_sends_what_was_left`, `test_a_shutdown_lets_the_appliers_finish_sends_their_decisions_once_and_closes_with_going_away` |
| `applier-no-task-done`: an applier never marks a message done, so the drain cannot see the queue finish | caught: `test_a_shutdown_lets_the_appliers_finish_sends_their_decisions_once_and_closes_with_going_away` |
| `drain-skips-flush`: the drain does not send the final messages the appliers committed | caught: `test_a_shutdown_lets_the_appliers_finish_sends_their_decisions_once_and_closes_with_going_away` |
| `flush-sends-tickets`: a draining flush also claims PENDING_SEND tickets | caught: `TestFlush::test_a_flush_sends_every_final_message_owed_and_no_ticket`, `test_a_draining_flush_claims_only_final_messages_owed_never_a_ticket`, `test_a_shutdown_lets_the_appliers_finish_sends_their_decisions_once_and_closes_with_going_away` |
| `drain-closes-normally`: a draining process closes its session with 1000 instead of 1001 | caught: `test_a_drain_that_runs_out_of_time_still_closes_and_a_successor_sends_what_was_left`, `test_a_shutdown_lets_the_appliers_finish_sends_their_decisions_once_and_closes_with_going_away`, `test_sigterm_drains_closes_with_going_away_and_exits_zero_with_json_logs` |
| `shutdown-waits-out-backoff`: a shutdown request without a session waits out the reconnect delay | caught: `test_a_shutdown_without_a_session_exits_without_waiting_out_the_backoff` |
| `sigterm-unhandled`: the event loop installs no SIGTERM handler, so SIGTERM exits without a drain | caught: `test_sigterm_drains_closes_with_going_away_and_exits_zero_with_json_logs` |
| `decision-time-from-now`: the time to decision is not measured from the ticket's creation | caught: `test_an_applied_decision_reports_how_long_the_customer_waited_on_db_time` |

Proposed commit (after review): `stage 5: Operability`, the whole working tree of the stage.

## Carry-over for the next stage

For stage 6 (README and docs):
- Alerts to describe (listed in D8): `auth_failed` endings increasing, no session open across pods for some minutes, `inbound_queue_full` endings increasing, compensation failures increasing, escalations and escalation failures. Mention a SQL exporter for database-wide backlog gauges.
- Known gaps for the README:
  - The monolith's services run under our `SET LOCAL` timeouts (`statement_timeout` 2 s, `lock_timeout` 1 s) and were not sized against real monolith operations.
  - The loss window for final messages.
  - Readiness has no session input; session status is a metric.
  - Resources are unmeasured.
  - SIGTERM is ignored during Django's setup when Python is PID 1.
- A question for the team in the README: how the monolith's web tier connects (WSGI direct, behind PgBouncer, ASGI) decides its own `DATABASE_CONN_MAX_AGE`; the shared settings default stays Django's 0 and only the integration Deployment sets it. The comments in `config/settings/base.py` and the command were reworded in stage 5 review so they no longer imply the web pods use 0.
- README section "Running on Kubernetes": the probe interface and wiring from D8 (probe → endpoint → period and threshold, grace period arithmetic, rollout strategy and session handover, per-Deployment `DATABASE_CONN_MAX_AGE`, secrets, unmeasured resources). There is no manifest.
- Run instructions: `make db-up`, `make check`; `make up`, then `make demo`, then `docker compose logs -f app` and `curl localhost:9100/metrics`; `docker compose stop app` shows the drain.
- `progress.md`, `stages.md` and `design-topics.md`: decide in the stage 6 design note what stays.
