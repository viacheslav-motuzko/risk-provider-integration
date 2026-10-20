# Code-level design topics

Top-level decisions are fixed (`decisions.md`). Code-level patterns are agreed with the user in the stage where they appear. When a stage touches one of these topics, put it in the design note with 2-3 options, trade-offs and a recommendation, then stop for the decision.

## Topics

- Layering and dependency direction; ports and adapters versus plain Django service functions.
- Where domain logic lives (pure domain module versus models).
- How much DDD is warranted (aggregate boundaries, value objects, domain errors).
- Persistence style: purpose-named functions versus repository classes versus managers/querysets and selectors.
- Transaction boundaries: who owns `atomic`, and how it interacts with `run_db`.
- The async/sync seam helper and the executor and connection lifecycle.
- Error taxonomy (transient versus permanent) and retry policy.
- Configuration, dependency wiring, testing strategy.
- The two-way coupling between `betting` (enqueues a ticket) and `risk_integration` (applies decisions through the adapter): keep it explicit, one-directional per module, and cheap to cut if the service is extracted.
- Typed integration settings: `pydantic-settings` with `SecretStr` versus plain parsing.
- Where the `run_db` helper lives and how persistence functions declare their transaction and timeouts.
- Shape of the applier queue items (raw frame versus decoded message) and where decode errors are counted.

## Leaning defaults (to confirm or overturn in discussion; not binding)

- Pure domain module with no Django or asyncio imports, enforced by import-linter.
- Ports only at real seams: monolith (`BetDecisionPort`), provider transport, clock (async timers only; domain code receives `now`). The event publisher port was dropped at the stage 1 gate (D1). No generic repository abstraction over the ORM.
- Django practices: service functions for use cases, selectors for reads, no signals for business flow, no logic in views.
- DDD-lite: `RiskTicket` is the aggregate and transitions go only through the state machine; no tactical-DDD ceremony.
- Manual wiring in one composition root (`runtime/app.py`); no DI framework.

## Decided (filled in stage 0 and later)

One line per topic: the decision, the reason, the date or stage. A topic listed here is not reopened without saying so at a gate.

- Clock source: DB time in SQL for every cross-pod comparison; `now` passed into domain code (D12, review round 1).
- Leader election: none. The provider's session policy decides how many sessions are active; correctness comes from D3 and A-03. A lease row is the documented upgrade path for kick-out with more than one replica (D6, revised in stage 0).
- Layering, domain, DDD, wiring: the leaning defaults above are adopted as written (stage 0).
- Seam between runtime and persistence: `allow_indirect_imports = true` on the runtime/transport contract only, so it forbids direct Django or ORM imports in async modules; Django's `SynchronousOnlyOperation` is the runtime guard. Reason: keeps the planned layout without a port per DB call (stage 0).
- Persistence style and transaction ownership: purpose-named module functions, no repository classes. `run_db` (in `persistence/db.py`) owns the unit of work: connection hygiene, `atomic`, `SET LOCAL` timeouts; the function runs inside that transaction. Monolith service functions keep their own `atomic`, which becomes a savepoint when called from the integration. Reason: one place for timeouts and transactions, nothing to forget (stage 0).
- `run_db` admission: a semaphore permit is released when the worker thread finishes, not when the awaiting coroutine times out; otherwise more threads than permits are busy (stage 0).
- Error taxonomy: CAS miss is a return value; transient errors are not retried inline, the applier included (revised at the stage 1 gate: retransmission recovers a failed apply); apply failures have three classes (D4, `ticket-lifecycle.md`); `OutcomeUnknown` for a timed-out `run_db`; permanent errors map to a definite outcome; unclassified exceptions fail fast. Known risk and remedy (poison message, per-item isolation) in D4. Reason: simplest behavior that never hides a bug (stage 0).
- Acks and re-send: no application acks (A-02). Final messages are re-sent after a reconnect within the loss window, whatever their type (D11, revised at the stage 1 gate). A correlated `ERROR` (`ref_type`) may correct a closed state (`ticket-lifecycle.md`). Reason: the task names no reply messages; this closes the realistic loss case with one query (stage 0).
- Ticket type: one, with one configured deadline stored per ticket as `deadline_at` (A-04). Reason: the task defines no ticket types (stage 0).
- Typed integration settings: `pydantic-settings` with prefix `RISK_`, no `env_file`, validated when the command starts, so web pods of the monolith need no provider variables (stage 0).
- Applier queue items: decoded messages; decode errors are counted in the reader (stage 0).
- `ticket_id`: a UUID we generate, separate from the bet's primary key (stage 0).
- Testing: pure unit tests for domain and codec (table of allowed and forbidden transitions); persistence on real Postgres, `transaction=True` when threads are involved; one fake provider (`tools/fake_provider`, in-process with a few knobs, stage 4); scenario tests run in pytest inside `make check`; no cross-process e2e suite (dropped at the stage 1 gate); no property-based testing (stage 0).
- Coupling between `betting` and `risk_integration`: one entry point per direction. `betting.place_bet` calls only `risk_integration.api.enqueue_ticket` inside its own transaction; the integration reaches the monolith only through `BetDecisionPort` and `adapters/monolith.py`. Enforced by the import contracts. Rejected: a signal or hook that creates the ticket (business flow in signals, implicit ticket creation) and a third coordinating module (one more module, nothing gained at this size). On extraction, the api call becomes an outbox event and the adapter a client (stage 3).
- Apply-failure classes (row 8 versus row 7): permanent means `IntegrityError`, `DataError` or `PermanentApplyError` (raised by the monolith adapter for a broken monolith invariant); transient means `OperationalError` (timeouts, deadlocks, serialization failures, lost connections), `InterfaceError` or `OutcomeUnknown`; everything else, `ProgrammingError` included, is unclassified and fails fast. `persistence/errors.py: classify_apply_failure`. Reason: a cancellation must never hide a bug (D4) (stage 2).
- `TICKET` payload snapshot: typed columns on the ticket row (`stake`, `currency`, `odds`, `selection_id`, `customer_ref`), not JSON. Reason: exact money, identical retransmissions, types checked by the DB (stage 2).
- Bet reference: plain unique `bet_id`, no foreign key. Reason: no migration dependency on `betting`, cheap to cut on extraction (stage 2).
- Applying a decision (rows 4-6, 8-10): lock, decide, transition. In one `run_db` unit: `lock_awaiting` takes `SELECT ... FOR UPDATE` on the ticket in its source states and returns `bet_id` (no row: duplicate or late, ignored); the port decides and applies; then the conditional transition, which cannot miss under the lock (a miss raises `LockedTransitionMissed`, a bug). `risk_integration/services.py`. Reason: one pattern for every money-moving row; the target state of an ACCEPT depends on the monolith's answer (row 4 or 6), and the limit check and the capture stay one port call. A duplicate or a racing cancellation waits for the lock (bounded by `lock_timeout`) and then finds nothing to do. Rejected: conditional update first with a read-only `check_accept` (two patterns, check and capture not atomic) and update first with a savepoint rollback on rejection (control flow through exceptions). Changes the stage 2 handoff ("conditional update first") (stage 3).
- The port runs inside the integration's transaction and under the ticket lock, so it does database work only: no network I/O, no waiting. A monolith operation that needs a remote call would need a saga (record intent, commit, call, record outcome), not this port. Lock order everywhere: ticket, bet, hold, wallet; `place_bet` inserts a new ticket and never waits on an existing one (stage 3).
- Monolith services own their `atomic`: called directly in the monolith it is the transaction; called through the adapter inside a `run_db` unit it is a savepoint of that unit, and an exception rolls the whole unit back (stage 3).
- Ticket deadline: a Django setting `RISK_TICKET_DEADLINE` (env `RISK_TICKET_DEADLINE_SECONDS`, default 10 s, A-04), not an integration setting, because the monolith's web pods create tickets (stage 3).
- Code comments: plain, short technical text for a non-obvious decision or trade-off; no IDs of decisions, assumptions or table rows in code, because a reviewer must not need the docs to read a comment. Provider guesses keep the greppable marker `# ASSUMPTION:` (stage 3 gate).
- `run_db` shape: a `DbRunner` instance built by the composition root and called as `await run_db(fn, *args)`; one set of timeouts per runner, no per-function override; explicit `shutdown()`. Reason: no global state, tests build their own runner (stage 2).
