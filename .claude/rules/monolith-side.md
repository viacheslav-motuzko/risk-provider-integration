---
paths:
  - "apps/betting/**"
  - "apps/wallets/**"
  - "apps/risk_integration/adapters/**"
  - "apps/risk_integration/api.py"
  - "apps/risk_integration/ports.py"
---

# Monolith side (stubs and the boundary)

- Wallet operations (hold, capture, release) are idempotent per bet: one `WalletHold` row per bet, changed by conditional update, no ledger. Release never touches a captured stake.
- Use-case functions own their `atomic`. Called through the adapter they run inside the integration's unit of work, so their `atomic` is a savepoint and an exception rolls back the whole unit.
- The port runs under the ticket lock: database work only, no network I/O, no waiting. Lock order everywhere: ticket, bet, hold, wallet.
- The adapter translates a broken monolith invariant (`BetStateError`, `HoldStateError`) into `PermanentApplyError`; a business rejection is a return value (`Rejected`), not an exception.
- `place_bet` creates the bet, the hold and the `PENDING_SEND` ticket in one transaction, through `risk_integration.api` only.
