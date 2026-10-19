---
paths:
  - "apps/betting/**"
  - "apps/wallets/**"
  - "apps/risk_integration/adapters/**"
  - "apps/risk_integration/api.py"
  - "apps/risk_integration/ports.py"
---

# Monolith side (stubs and the boundary)

- Money is `Decimal`, never `float`. Wallet operations (hold, capture, release) are idempotent per bet.
- Use-case functions own their `atomic` block. Accepting, rejecting or voiding a bet is one transaction including the wallet effect.
- `betting` and `wallets` import only `risk_integration.api`. `risk_integration` imports `betting` and `wallets` only in `adapters/monolith.py`.
- `place_bet` creates the bet and the `PENDING_SEND` ticket in the same transaction.
- Anything that is a stub is named `Stub*` or clearly marked, and logs that it is a stub at startup. It must not pretend to be real.
- Event publishing goes through the publisher port. The only implementation here logs from `transaction.on_commit` and is explicitly best-effort (D1); the transactional event outbox is a README next step.
