---
paths:
  - "apps/risk_integration/persistence/**"
  - "apps/risk_integration/models.py"
  - "apps/risk_integration/migrations/**"
  - "apps/risk_integration/services.py"
---

# Persistence

- Functions here run inside the caller's transaction: a `run_db` unit, or the monolith's `atomic` for `create_ticket`. Never open a transaction across an `await`.
- Claim with `select_for_update(skip_locked=True)`, write the lease, commit, then send. An expired lease is claimable by the claim query itself; every transition clears the lease.
- Every state change is a conditional update built from `RULES` and returns whether it applied; "did not apply" is a normal outcome for the caller.
- Money-moving transitions use one pattern in one unit of work: `lock_awaiting` (row lock in the source states, also reads whether the deadline passed), then the port, then the transition, which cannot miss under the lock (`LockedTransitionMissed` is a bug). A recommendation past `deadline_at` is ignored and left to the sweeper.
- Time comparisons use `Now()` in queries and `statement_timestamp()` in raw SQL, never the pod's clock.
- Every claim, retransmit, deadline and re-send query is served by a partial index; a comment on the index names the queries.
- Migrations: reversible, one per model change, no data-heavy steps; enum values are written as `.value`, not as the enum.
