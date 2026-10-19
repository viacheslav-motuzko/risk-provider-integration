---
paths:
  - "apps/risk_integration/domain/**"
  - "apps/risk_integration/protocol/**"
---

# Domain and protocol packages

- `domain` imports the standard library only: no Django, asyncio, websockets or pydantic.
- `protocol` imports the standard library and pydantic only: no Django, no ORM, no sockets.
- State changes go only through the transition table in `domain/machine.py`. Transition functions are pure and receive `now` as an argument; they never read a clock (D12).
- Malformed, unknown or unexpected frames produce typed errors; the caller decides what to do. Never raise bare exceptions from the codec.
- The protocol layer is the anti-corruption layer for provider assumptions: every guess about the provider's wire format lives here and carries an `# ASSUMPTION A-xx` reference, so a wrong guess is fixed in one place.
