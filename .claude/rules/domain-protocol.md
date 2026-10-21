---
paths:
  - "apps/risk_integration/domain/**"
  - "apps/risk_integration/protocol/**"
---

# Domain and protocol packages

- `domain` imports the standard library only; `protocol` the standard library and pydantic only (import-linter enforces both).
- State changes go only through the transition table in `domain/machine.py`. The domain never reads a clock: deadlines and leases are compared in SQL on DB time.
- The reason enums exist in both packages with the same spelling; the runtime maps them by value. Change both together.
- Malformed, unknown or unexpected frames raise `FrameError` with a bounded `kind`; the codec raises nothing else. Inbound models ignore unknown fields; outbound models forbid them.
- `protocol` is the anti-corruption layer: every guess about the provider's wire format lives here, marked `# ASSUMPTION:`, so a wrong guess is fixed in one place.
