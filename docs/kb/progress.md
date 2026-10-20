# Progress and handoff

Written by the agent at the end of every stage and read at the start of the next one (sessions do not share context). Keep it short and factual. Folded into the README or removed in stage 6.

## Status

| Stage | Branch | State | Merged into main |
|-------|--------|-------|------------------|
| 0. Bootstrap | `stage/0-bootstrap` | done; scaffold and stage 0 docs in one commit by the user | yes |
| 1. Provider contract | `stage/1-protocol` | done, incl. the gate revision | yes |
| 2. Domain and persistence | `stage/2-domain-persistence` | done, reviewed, committed | yes |
| 3. Monolith side | `stage/3-monolith` | done, awaiting review (uncommitted) | no |
| 4. Runtime core and failure scenarios | `stage/4-runtime` | not started | no |
| 5. Operability | `stage/5-ops` | not started | no |
| 6. Docs and polish | `stage/6-docs` | not started | no |

## Latest summary

Stage 3 (monolith side). Gate decisions: coupling 1(a), apply pattern 2(a) (recorded in `design-topics.md`, "Decided", stage 3).

- Done:
  - `wallets` (STUB): `Wallet` (balance per customer and currency, check `balance >= 0`) and `WalletHold` (one per bet, unique `bet_id`, `HELD`/`CAPTURED`/`RELEASED`). In `services.py`:
    - `hold` debits by a conditional update (`balance >= amount`) and raises `InsufficientFunds`, also when the wallet is missing.
    - `capture` is idempotent and refuses a released or missing hold.
    - `release` credits once and returns whether it released; a missing hold has nothing to release. It refuses a captured stake.
    - `fund` exists for the demo and tests. No ledger.
  - `betting` (STUB): `Bet` (`PENDING_RISK`/`ACCEPTED`/`REJECTED`/`VOIDED`). In `services.py`:
    - `place_bet`: the bet, the hold and `api.enqueue_ticket` in one `atomic`.
    - `accept_bet` returns `None` or a typed `Rejection`. The stub payout limit (`STUB_MAX_POTENTIAL_PAYOUT`, A-08) rejects and releases.
    - `reject_bet`.
    - `void_bet`: compensation. It is idempotent on a bet already voided or rejected and refuses an accepted bet.
    - Demo command `place_demo_bet`.
  - `risk_integration`:
    - `api.py`: `enqueue_ticket`, `TicketSnapshot`; the deadline comes from the Django setting `RISK_TICKET_DEADLINE`.
    - `ports.py`: `BetDecisionPort`, `Accepted`, `Rejected(DeclineReason)`.
    - `adapters/monolith.py`: `DjangoBetDecisionAdapter`; `BetStateError`/`HoldStateError` become `PermanentApplyError`.
    - `services.py`: `apply_recommendation` (rows 4-6), `cancel_ticket` (rows 8-10). Both are sync, one `run_db` unit each: lock, decide, transition.
    - `persistence/tickets.py`: `lock_awaiting(ticket_id, past_deadline=...)`.
    - `domain/states.py`: `DeclineReason`.
  - Tests: 286 total, 42 new.
    - Placement atomicity: a failed ticket insert leaves no bet, hold or debit; insufficient funds or a missing wallet creates nothing.
    - Each decision with its money:
      - accept captures; decline releases;
      - a domain rejection declines with `LIMIT_EXCEEDED` and releases;
      - the same from `PENDING_SEND` (the reply beat the relay's commit).
    - Duplicates and late recommendations move no money.
    - Deadline cancellation:
      - not due: a miss, nothing moves;
      - due: compensation and `CANCEL_PENDING` together.
    - Refused ticket: compensation, and the error is kept.
    - No refund:
      - a cancellation after acceptance is ignored;
      - compensation refuses an accepted bet. The two guards (bet, then wallet) are tested separately.
    - Row 8: a broken invariant (hold released or missing) is `PERMANENT`, the failed unit rolls back, and the new `APPLY_FAILED` unit compensates.
    - Row 7: a failed transition after the monolith effect rolls the effect back (`TRANSIENT`).
    - Real `run_db`: a decision commits with its effect; a failed unit rolls the effect back.
    - Threads:
      - two appliers with a duplicate decline credit the stake once;
      - accept versus deadline cancellation, each order: exactly one wins, and the money matches the winner.

      The second unit is proven to wait for a lock (it shows up as a lock waiter in `pg_stat_activity`); no sleeps, no barrier luck.
    - Wallet operations; a smoke test of the demo command.
  - `make check` and `make mutate` green: 28 sabotages, 9 new, all caught (table below).
- Review round 1 (user reviewed the code; fixes in the working tree, still uncommitted):
  - The user's own edits to wording and naming
  - A second, exploratory round of nine extra sabotages: eight caught by existing tests. One survived: a transition that misses under the decision lock, if not raised, would commit the monolith effect (bet accepted, stake captured) while the ticket stayed `SENT`. Added `test_a_transition_that_misses_under_the_lock_fails_fast_and_rolls_back_the_monolith_effect`, `test_the_decision_lock_takes_exactly_the_source_states_of_its_transitions` (the drift that would cause such a miss), and the `locked-miss-silent` entry. The other eight were not added to `make mutate`: they duplicate existing entries in effect (each is caught by a direct test of the same money movement).
- Deviations:
  - The apply pattern is lock, decide, transition, instead of the stage 2 carry-over's "conditional update first" (decided at the gate). Every transition is still a conditional update. Under the lock a miss is a bug: `LockedTransitionMissed` (unclassified, fails fast).
  - New module `risk_integration/services.py` and a top-level `tests/` package for tests that cross `betting`, `wallets` and `risk_integration`. Outside both packages, the import contracts stay strict. `PROJECT_STRUCTURE.md` updated.
  - `record_domain_rejection` takes `DeclineReason` instead of `str`; two stage 2 tests updated. `DeclineReason` mirrors `protocol` by value, like `CancelReason`.
  - The ticket deadline is a Django setting (web pods create tickets), not part of the `RISK_` integration settings of stage 4.
  - The carry-over said to drop `Clock` from `ports.py`. `PROJECT_STRUCTURE.md` never listed it there, so nothing to drop.
  - The stage 2 summary describes three check constraints (including "escalated has a reason") and migration `0001_risk_ticket`. The committed code has two constraints and `0001_initial`. I took the committed code as intended and changed nothing.
- Assumptions added: none with an ID.
  - E-09 is extended: monolith operations are local DB operations with no network calls, because they run inside the integration's transaction.
  - D7 gains a stage 3 bullet: compensation is idempotent and refuses an accepted bet.
- Gate decisions (user, after review round 1):
  - D11 reworded: a recommendation after the deadline cancellation is ignored; one after the deadline but before the sweeper's pass is applied. `ticket-lifecycle.md` matches.
  - A compensation that meets an accepted bet fails again on every sweeper pass: accepted, with an alert (stage 4).
  - A duplicate that waits on the ticket lock past `lock_timeout` gets its own metric label (stage 4).
  - All proposed gotchas (stages 1-3) approved and moved into `gotchas.md`; D12's clock wording corrected (`Now()` is `statement_timestamp()`).
  - One commit per stage (`CLAUDE.md`, `.claude/commands/stage.md` and `stages.md` updated).
  - New comment policy: no IDs of decisions, assumptions or table rows in code; a non-obvious decision or trade-off is described in short technical text; provider guesses keep the marker `# ASSUMPTION:`. `CLAUDE.md`, `.claude/rules/domain-protocol.md`, `stages.md` (definition of done), `PROVIDER_QUESTIONS.md`, `provider-assumptions.md`, `design-topics.md` updated. Applied to all code written so far (stages 1-3, about 50 comments and docstrings, `pyproject.toml` included); no behaviour change.
- How to review:
  1. `apps/risk_integration/services.py`, `ports.py`, `adapters/monolith.py`, and `lock_awaiting` in `persistence/tickets.py`: the pattern and the error translation.
  2. `apps/betting/services.py`, `apps/wallets/services.py`, the models and both migrations: conditional updates, idempotency, the refusals.
  3. `api.py`, the setting in `config/settings/base.py`.
  4. `tests/test_bet_flow.py`, `tests/test_bet_flow_concurrency.py` (`_race`), `apps/wallets/tests/`.
  5. `git status` for new files; `git diff` for the doc edits and the mutation entries.

`make mutate` result for the entries added in this stage (the 19 stage 2 entries are still caught; full table from `make mutate`):

| Sabotage | Caught by |
|---|---|
| deciding does not lock the ticket | both race tests, `test_duplicate_recommendation_applied_by_two_appliers_at_once_moves_money_once` |
| the deadline cancellation compensates a ticket that is not due | `test_deadline_cancellation_compensates_in_the_same_transaction` |
| `place_bet` creates the ticket after the bet's transaction | `test_place_bet_leaves_nothing_behind_when_the_ticket_cannot_be_created` (+3) |
| a domain rejection does not release the stake | `test_accept_over_our_limit_is_declined_and_releases_the_stake` |
| compensation voids an accepted bet | `test_compensation_refuses_to_void_an_accepted_bet[HELD]` |
| release is not conditional on `HELD` (double credit, refund) | `test_release_returns_the_stake_once`, `test_a_captured_stake_is_never_released` |
| capture is not conditional on `HELD` | `test_broken_monolith_invariant_is_permanent_and_compensation_still_completes`, `test_capture_refuses_a_hold_that_is_not_held` |
| a transition that misses under the decision lock is not raised | `test_a_transition_that_misses_under_the_lock_fails_fast_and_rolls_back_the_monolith_effect` |
| the adapter does not translate a broken monolith invariant | `test_broken_monolith_invariant_is_permanent_and_compensation_still_completes`, `test_compensation_refuses_to_void_an_accepted_bet` |

Proposed commit (after review): `stage 3: Monolith side`, the whole working tree of the stage.

## Carry-over for the next stage

Open questions, deviations to remember, assumptions added (IDs), proposed gotchas not yet moved to `gotchas.md`.

For stage 4:
- Applier: `await run_db(services.apply_recommendation, port, ticket_id, decision)`; `None` means ignored (log and count). On an exception, `classify_apply_failure`: `None` re-raises (fail fast), `TRANSIENT` leaves the ticket, `PERMANENT` runs `await run_db(services.cancel_ticket, port, ticket_id, Trigger.APPLY_FAILED)`. A `PermanentApplyError` from that compensation unit (accepted bet) is logged, counted and alerted, and the ticket is left as it is; the sweeper retries it every pass (decided at the stage 3 gate).
- Sweeper: `find_expired`, then `services.cancel_ticket(port, id, Trigger.DEADLINE_PASSED)` per ticket. `False` is a normal miss. Errors from the provider about a `TICKET` go to `cancel_ticket(..., Trigger.TICKET_REFUSED, error_code=...)`; confirm and cancellation refusals call `tickets.escalate` directly (no money moves).
- A duplicate that waits on the ticket lock past `lock_timeout` raises `OperationalError` (transient, harmless): give it its own metric label so it does not read as an apply failure (stage 3 gate).
- The composition root builds `DjangoBetDecisionAdapter` and logs at startup that the monolith behind it (`betting`, `wallets`) is a stub (monolith-side rule).
- Relay: `claim_due` on every poll; after a (re)connect, take `since = db_now()` and page `claim_sent_for_resend` and `claim_finals_for_resend` until empty; after each send, `record_send(ticket_id, claimed.state, ...)`. Map `CancelReason`/`MessageRef` to protocol enums by value.
- Open question for the gate: D6 makes `AUTH_FAILED` fatal (the process exits). An auth outage on the provider side would then crash-loop every pod, which conflicts with the spirit of invariant 7. Alternative: long backoff, a session-status metric and an alert, with no exit. Recommendation: long backoff plus alert.
- The reader catches `FrameError` and counts `kind` as the metric label. It logs `message_type` and `ticket_id` from the error.
- `encode` needs an aware `ts`: pass wall-clock UTC. It is informational only (D12).
- Open point for the design note: the loss-window anchor (`window_start` of `claim_finals_for_resend`) is the disconnect time, which a freshly started process does not know. Candidate: anchor on `max(last_sent_at)` of closed tickets at the first connect of a process.
- The fake provider needs the opposite direction (decode outbound, encode inbound). Either it builds JSON from the same `Envelope` model, or the codec gains two test-facing helpers. Do not put fake-only code in `protocol/` without saying so.
- `.env.example` and compose carry a static `PROVIDER_WS_TOKEN`; rename to the `RISK_` prefix when the integration settings are written. The settings also size `DbRunner` (workers = applier count, D4) and its timeouts.
- Call `DbRunner.shutdown()` off the loop, before the loop closes.
- For the design note: `run_db` bounds only the work, not the wait for admission. A caller can wait for a permit without limit, which breaks "every DB operation has an explicit timeout" (invariant 5, async-runtime rule). Proposal: bound the admission wait and raise a separate error for it (for example `NotAdmitted`). Its outcome is known (the work never ran), so it is transient and needs no re-read, unlike `OutcomeUnknown`. Also, no `connect_timeout` is set in the database options, so connecting to an unreachable DB can hang for the TCP timeout. Set it with the integration settings. Both found in stage 2 by the mutation review.
- For the design note, sharing the database with the monolith (raised in the stage 3 review discussion):
  - `config/settings/base.py` sets `conn_max_age=0`, so the integration opens a new connection for every unit of work, poll included: tens per second per pod. A transaction pooler (E-06) absorbs that; direct Postgres forks a backend per connect. The integration's thread pool is fixed, so persistent connections (`CONN_MAX_AGE` > 0, `CONN_HEALTH_CHECKS=True`) in the integration settings give exactly `max_workers` steady connections; `close_old_connections` in `DbRunner` already honors it.
  - The monolith's services run under our `SET LOCAL` `statement_timeout` and `lock_timeout`. Size them against the monolith's real operations (a real wallet may post to a ledger), not only against our own queries.
  - A slow DB slows the appliers, the bounded queue fills, the reader pauses, pongs go unread and the keepalive closes the WebSocket (gotcha on `max_queue`). No loss, but reconnects and re-sends arrive at the worst moment. Label this disconnect cause separately, or keep handling pongs while the queue is full.
- `config/settings/base.py` raises a bare `KeyError` when `DJANGO_SECRET_KEY` or `DATABASE_URL` is missing. Acceptable fail-fast; revisit with the startup probe in stage 5.
