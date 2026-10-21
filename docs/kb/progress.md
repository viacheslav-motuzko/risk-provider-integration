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
| 4b. Session resilience | `stage/4b-session` | done, awaiting review (uncommitted) | no |
| 5. Operability | `stage/5-ops` | not started | no |
| 6. Docs and polish | `stage/6-docs` | not started | no |

## Latest summary

Stage 4b (session resilience). Gate decision at the design note: the relay releases its leases when its session ends, by primary key on its current batch, instead of releasing every lease of the owner at session start (which would have needed a new partial index). Recorded in D3 and `design-topics.md`.

- Done:
  - `runtime/connection.py`: the connection task holds a session for as long as the process runs. On each pass it fetches the token, connects, runs the session group, and when the session ends or is refused, waits by outcome and connects again. Only a bug ends it. `session_ended` now carries `connection_id`, how long the session lasted, and whether the inbound queue was full. `AUTH_FAILED` is logged at error level.
  - `runtime/backoff.py` (new, pure): the delay per outcome.
    - `TRANSIENT`: 0.5 s doubling to 30 s.
    - `PLANNED`: at once if the session was stable (60 s), otherwise transient.
    - `ALREADY_CONNECTED`: 1-2 s, never growing.
    - `DISPLACED`: 30-60 s.
    - `AUTH_FAILED`: 30 s doubling to 5 min.
    - Every delay is jittered down by up to half. A streak is endings with the same outcome in a row; a stable session or a new outcome starts a new streak, a handshake alone does not.
  - `transport/ws_client.py`: `fetch_token(within=...)`, a timeout is `TRANSIENT`. The `TokenProvider` contract says a real one raises `AUTH_FAILED` for refused credentials.
  - `runtime/relay.py`:
    - On the way out (failed send or cancellation), the relay releases its current batch's leases.
    - A claim still running when the relay is cancelled is waited out (bounded by the `run_db` await timeout) and released too. See Deviations.
  - `persistence/tickets.py`: `release_leases(ticket_ids, owner=)`, by primary key and owner.
  - `runtime/metrics.py`:
    - `risk_session_open` gauge.
    - `risk_sessions_ended_total` gets an `inbound_queue_full` label: the queue was full when the session ended, i.e. the appliers had stopped the reader and pongs went unread.
  - `runtime/config.py`: `RISK_TOKEN_TIMEOUT`, `RISK_RECONNECT_INITIAL`, `RISK_RECONNECT_MAX`, `RISK_STANDBY_RETRY`, `RISK_DISPLACED_BACKOFF`, `RISK_AUTH_BACKOFF_INITIAL`, `RISK_AUTH_BACKOFF_MAX`, `RISK_SESSION_STABLE_AFTER`. Each growing delay is validated so it cannot start above its cap.
  - `tools/fake_provider/server.py`:
    - `reject_new` knob: `UPGRADE` refuses with 409, `CLOSE` closes with 4003 after the handshake.
    - The accepted `token` can change at runtime.
    - `sessions_opened` counter.
  - Tests: 348 in total, 23 more than after 4a; the two 4a session tests rewritten.
    - Scenarios (real loops, real Postgres, fake over a real socket):
      - a planned close of a stable session reconnects at once (any other delay is set to 60 s) and keeps working;
      - a dropped session reconnects and re-sends `SENT` tickets and recent final messages;
      - a refused token keeps the process alive and retrying, and it connects once the token is accepted;
      - a refused second instance stands by, then takes over the first one's `SENT` ticket after it stops; run once for 409 and once for 4003;
      - appliers that fall behind stop the reader, the keepalive drops the session, it is labelled `inbound_queue_full`, and the retransmission recovers the ticket after the reconnect.
    - Units:
      - the backoff per outcome, streaks, caps and jitter;
      - a hanging token fetch is transient;
      - lease release: on a session that dies mid-batch, on a relay cancelled mid-send, and on a claim that commits after the cancellation;
      - `release_leases` frees only the given rows of the given owner.
  - `make check` green. `make mutate`: 56 sabotages, 13 new, all caught (table below).
- Deviations:
  - **A flaw found while testing**: a claim in flight when the relay is cancelled. The design note named "a claim commits after the cancellation" as a hole the lease would cover. The new backpressure scenario failed about 1 run in 20 because of exactly that: the worker thread committed a `claim_due` after the session group was cancelled, which took the freshly `CONFIRM_PENDING` row with a 6 s lease that nobody sent. Every session that ends during a relay poll can hit it. `Relay._claim` now shields the claim, waits it out on cancellation and releases what it took. After the fix: 0 failures in 60 runs. A deterministic unit test holds the claim inside its unit of work across the cancellation; it fails 5 of 5 without the fix. Recorded in D3.
  - `supervise` (4a) is typed `NoReturn`, and an empty mapping raises `LoopReturned`. Without that, mypy cannot see that the session group never returns.
  - The 4a relay unit test was moved into `TestRelay` with a `run_db` fixture and `make_relay`; `_DeadSession` became `_SessionThatDies(frames=)`. Same assertions.
  - `tests/scenario_helpers.py`: `running(...)` takes settings overrides, test backoffs are short, and `Running.stop()` stops one instance.
  - D4 got one clause ("the connection task connects again"). D6 rewritten for the outcome bullet (AUTH_FAILED no longer fatal, the numbers, streaks, token timeout, session metrics). D3 got the release bullet with the considered options.
  - No new dependency, no migration. `.env.example` unchanged: the new variables have defaults.
- Assumptions added: none with an ID. The token contract (`AUTH_FAILED` for refused credentials, transient otherwise) is part of A-11.
- Open questions for the gate:
  1. **Backoff defaults** (above). Every number is configurable; the ones I would most expect to change are `DISPLACED` (30-60 s) and the auth cap (5 min).
  2. **`ws_close_timeout` delays noticing a dropped session.** After a keepalive failure, websockets waits for the close handshake, up to `close_timeout` (10 s by default), before the session ends, and an unread socket never completes that handshake. The relay's sends fail sooner (send timeout 2 s), but the reconnect starts only then. Proposal for stage 5, together with the drain budget: lower `RISK_WS_CLOSE_TIMEOUT` to about 2 s.
  3. **`inbound_queue_full`** is the queue sampled when the session ended, not proof of the cause; an applier can free a slot in between. It is a diagnostic label, named after what it measures.
- Proposed gotchas:
  - Cancelling a `run_db` caller does not stop its unit of work, so a claim can commit after the cancellation and hold rows for a whole lease with nobody to send them. Shield the claim, wait it out on cancellation, release what it took. (Extends the existing executor gotcha.)
  - websockets: after a keepalive failure the connection is reported closed only after `close_timeout`, when the peer's close frame cannot be read (the reader is paused). Tests that drop a session through the keepalive must shorten `close_timeout` too.
  - `return`, `break` and `continue` are not allowed in an `except*` block. Re-raise, and catch the plain exception one level up.
  - mypy reports "Missing return" after `try: async with ...` unless the body calls a function typed `NoReturn`.
- How to review:
  1. `runtime/backoff.py` and `tests/test_runtime_backoff.py`: the policy per outcome.
  2. `runtime/connection.py`: the reconnect loop, what is logged and counted per ending.
  3. `runtime/relay.py`: `run`'s `finally`, `_claim` (the shield), `_release`; `persistence/tickets.py: release_leases`.
  4. `transport/ws_client.py` (`fetch_token`), `runtime/config.py`, `runtime/app.py`, `runtime/metrics.py`, `runtime/supervisor.py`.
  5. `tests/test_runtime_scenarios.py` ("The session" section), `apps/risk_integration/tests/test_runtime_units.py` (`TestRelay`), `tools/fake_provider/server.py`, `tests/scenario_helpers.py`.
  6. `git diff` of `decisions.md` (D3, D4, D6), `design-topics.md`, `PROJECT_STRUCTURE.md`, `tools/mutation_check.py`.

`make mutate` result for the entries added in this stage (the 43 earlier entries are all still caught):

| Sabotage | Caught by |
|---|---|
| `session-end-exits`: a session that ends or is refused ends the process instead of reconnecting | caught: `test_a_dropped_session_reconnects_and_resends_sent_tickets_and_recent_final_messages`, `test_a_planned_close_of_a_stable_session_reconnects_at_once_and_carries_on`, `test_a_refused_second_instance_stands_by_and_takes_over_the_first_ones_sent_tickets[...]`, `test_a_refused_token_keeps_the_process_retrying_until_the_provider_accepts_it`, `test_appliers_that_fall_behind_stop_the_reader_and_the_dropped_session_is_labelled_then_recovered` |
| `backpressure-unlabelled`: a session dropped while the inbound queue was full is not told apart | caught: `test_appliers_that_fall_behind_stop_the_reader_and_the_dropped_session_is_labelled_then_recovered` |
| `planned-not-immediate`: a planned close of a stable session waits like a transient failure | caught: `test_a_planned_close_of_a_stable_session_reconnects_at_once_and_carries_on`, `test_a_planned_close_of_a_stable_session_reconnects_at_once_every_time` |
| `planned-always-immediate`: a planned close reconnects at once even after a session that did not last | caught: `test_a_planned_close_of_a_short_session_backs_off_like_a_transient_one` |
| `displaced-immediate`: a displaced session reconnects at once | caught: `test_a_displaced_session_waits_long_before_it_reconnects` |
| `standby-grows`: a refused standby backs off further and further instead of retrying soon | caught: `test_a_standby_retries_soon_and_never_slower` |
| `auth-short-backoff`: a refused token is retried on the short transient curve | caught: `test_a_refused_token_waits_long_and_longer_up_to_the_cap` |
| `streak-reset-at-handshake`: any opened session resets the backoff, so accept-then-close loops fast | caught: `test_a_handshake_alone_does_not_reset_the_streak_but_a_stable_session_does`, `test_a_planned_close_of_a_short_session_backs_off_like_a_transient_one` |
| `streak-across-outcomes`: a new outcome continues the previous streak (a standby that took over drops to the cap) | caught: `test_a_new_outcome_starts_a_new_streak` |
| `token-unbounded`: the token fetch has no timeout | caught: `test_a_token_fetch_that_hangs_ends_the_attempt_as_transient` |
| `leases-kept-at-session-end`: a relay whose session ends keeps the leases of its unsent batch | caught: `TestRelay::test_a_relay_cancelled_mid_send_leaves_no_lease_on_its_batch`, `TestRelay::test_a_session_that_dies_mid_batch_leaves_no_lease_on_the_rest_of_it` |
| `cancelled-claim-kept`: a claim that commits after the relay is cancelled keeps its leases | caught: `TestRelay::test_a_claim_that_commits_after_the_relay_is_cancelled_is_released_too` |
| `release-any-owner`: releasing a batch also frees leases another relay holds | caught: `test_releasing_a_batch_frees_only_its_own_leases_and_leaves_recorded_rows_alone` |

Proposed commit (after review): `stage 4b: Session resilience`, the whole working tree of the stage.

## Carry-over for the next stage

Open questions, deviations to remember, assumptions added (IDs), proposed gotchas not yet moved to `gotchas.md`.

For stage 5:
- Serve `runtime/metrics.py` and add the gauges from the ops rule (time to decision, oldest unresolved ticket, tickets by state, outbox depth, queue depth, session status). Heartbeats: one bump per loop iteration in the relay, appliers, sweeper and connection.
- structlog JSON rendering, UTC timestamps; keep the websockets logger at INFO or above.
- Drain on SIGTERM: the relay stops claiming `PENDING_SEND`, the reader stops, the appliers finish the queue (add `task_done`/`join`), `*_PENDING` is flushed once, the session closes with 1001 (today `ws.close()` sends 1000).
- The integration Deployment sets `DATABASE_CONN_MAX_AGE` (for example 60); web pods keep 0.
- `tools/fake_provider/__main__.py` for compose.
- The monolith's services run under our `SET LOCAL` `statement_timeout` (2 s) and `lock_timeout` (1 s); size them against the monolith's real operations.
- `config/settings/base.py` raises a bare `KeyError` when `DJANGO_SECRET_KEY` or `DATABASE_URL` is missing; revisit with the startup probe.
- Readiness ("can take over"): the connection task and `risk_session_open` exist; readiness must not require holding the session (a standby under reject-new is ready).
- Alerts to describe: `risk_sessions_ended_total{outcome="auth_failed"}` increasing; `sum(risk_session_open) == 0` for some minutes across pods; `risk_sessions_ended_total{inbound_queue_full="true"}` increasing (appliers behind the provider).
- `RISK_WS_CLOSE_TIMEOUT` (open question 2) together with the drain budget and `terminationGracePeriodSeconds`.
- The relay already releases its batch's leases when its session ends; the drain can rely on that.
