---
paths:
  - "apps/risk_integration/persistence/**"
  - "apps/risk_integration/models.py"
  - "apps/risk_integration/migrations/**"
---

# Persistence

- This package and `models.py` are the only places in `risk_integration` that use the ORM.
- Claim work with `select_for_update(skip_locked=True)` inside a short `atomic` block, write the lease (`locked_by`, `locked_until`), commit, and only then do network I/O.
- Every state change is a conditional update on the expected state (and lease owner where relevant) and returns whether it applied. Callers must handle "did not apply" as a normal outcome (duplicate, late, lost lease).
- A transaction that applies a recommendation includes the monolith effects and the ticket transition together, through the port; it either all commits or none of it does.
- Time comparisons that matter across pods (ticket leases, deadlines) are done in SQL against DB time (D12).
- Each unit of work sets `SET LOCAL statement_timeout`, `lock_timeout` and `idle_in_transaction_session_timeout`. Nothing session-level: no advisory locks, no `LISTEN/NOTIFY`, no session `SET` (E-06).
- Add partial indexes on the non-terminal states for the claim, retransmit and sweep queries, and say in the stage note which queries they serve.
- Migrations are reversible, one per model change, with no data-heavy steps.
- Do not hold a transaction open across an `await` or any I/O. `atomic` blocks live inside sync functions executed by `run_db`.
