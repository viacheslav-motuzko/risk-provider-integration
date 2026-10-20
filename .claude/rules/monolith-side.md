---
paths:
  - "apps/betting/**"
  - "apps/wallets/**"
  - "apps/risk_integration/adapters/**"
  - "apps/risk_integration/api.py"
  - "apps/risk_integration/ports.py"
---

# Monolith side (stubs and the boundary)

- Money is `Decimal`, never `float`. Wallet operations (hold, capture, release) are idempotent per bet: one `WalletHold` row per bet, changed by conditional update, no ledger. Compensation only voids the bet and releases a held stake; nothing refunds a captured stake (B-01).
- Use-case functions own their `atomic` block. Accepting, rejecting or voiding a bet is one transaction including the wallet effect.
- `betting` and `wallets` import only `risk_integration.api`. `risk_integration` imports `betting` and `wallets` only in `adapters/monolith.py`.
- `place_bet` creates the bet and the `PENDING_SEND` ticket in the same transaction.
- Anything that is a stub is named `Stub*` or clearly marked, and logs that it is a stub at startup. It must not pretend to be real.
- No event publisher (D1): domain events through a transactional outbox are a README next step.
