# Progress and handoff

Written by the agent at the end of every stage and read at the start of the next one (sessions do not share context). Keep it short and factual. Folded into the README or removed in stage 7.

## Status

| Stage | Branch | State | Merged into main |
|-------|--------|-------|------------------|
| 0. Bootstrap | `stage/0-bootstrap` | done; scaffold and stage 0 docs in one commit by the user | yes |
| 1. Provider contract | `stage/1-protocol` | done, awaiting gate | no |
| 2. Domain and persistence | `stage/2-domain-persistence` | not started | no |
| 3. Monolith side | `stage/3-monolith` | not started | no |
| 4. Runtime core | `stage/4-runtime` | not started | no |
| 5. Operability | `stage/5-ops` | not started | no |
| 6. Fake provider and failures | `stage/6-fake-provider` | not started | no |
| 7. Docs and polish | `stage/7-docs` | not started | no |

## Latest summary

Stage 1 (provider contract).

- Done:
  - `docs/PROVIDER_QUESTIONS.md`: every A-xx entry written out as a question to the provider, with the assumed answer, why it matters and what changes if the answer differs, grouped by impact (correctness, timing, sessions, wire format, capacity). B-xx entries are in a separate section.
  - `protocol/` package (pydantic + stdlib):
    - `envelope.py` with an optional `ticket_id` and optional `ts`.
    - `messages.py`: frozen models. Outbound models forbid extra fields; inbound models ignore unknown fields.
    - `codec.py`: `encode(msg, *, ts) -> str`; `decode(frame)` raises only `FrameError` with a bounded `kind` (`binary`, `not_json`, `bad_envelope`, `unknown_type`, `bad_payload`), plus type and `ticket_id` when readable; input values never go into the error detail.
    - `close_codes.py`: `classify_close(code | None)` and `classify_handshake_rejection(status)` return `AUTH_FAILED / DISPLACED / ALREADY_CONNECTED / PLANNED / TRANSIENT`. Unknown values are transient.
  - 71 unit tests: exact fixed-point decimals, envelope precedence, tolerant decoding, every malformed-frame kind, no input echo, the classification table including kick-out versus reject-new. `make check` green.
- Assumptions added: A-16 (`TICKET` payload, decline and cancellation reason enums), A-17 (HTTP statuses and close codes for session outcomes), B-05 (singles only; the wire format carries a list). The A-02 entry now names `UNKNOWN_TICKET` as the only error code that changes our reaction; other codes are kept raw.
- Deviations / scaffold fixes:
  - Added `apps/__init__.py`. Without it, mypy saw the same file under two module names once modules imported each other.
  - `PROJECT_STRUCTURE.md`: inbound `Error` is named `ProviderError`, and `close_codes.py` adds `PLANNED` (A-14).
- How to review:
  - Read `docs/PROVIDER_QUESTIONS.md` (A-16, A-17, B-05 are new).
  - Then `apps/risk_integration/protocol/` (about 250 lines) and the two test files `tests/test_protocol_*.py`.
  - `git log main..stage/1-protocol` shows the commits.

## Carry-over for the next stage

Open questions, deviations to remember, assumptions added (IDs), proposed gotchas not yet moved to `gotchas.md`.

For stage 2:
- Persist the cancellation reason on the ticket: `RequestCancellation.reason` is required. A re-send after a reconnect (D11) must carry the same reason, and it must come from a column, not from memory. Mapping: sweeper at the deadline gives `DEADLINE_EXCEEDED`; a permanent apply failure gives `APPLY_FAILED`; `ERROR` about `TICKET` in `SENT` gives `TICKET_REJECTED`; `ERROR` about `CONFIRM` in `CONFIRMED` gives `CONFIRM_REJECTED`.
- `last_error_code` stores `ProviderError.code` raw. Treat a cancellation as done when `ProviderError.is_unknown_ticket` is true (A-09).
- Ticket fields beyond the seed: `recommendation`, `last_error_code`, `last_error_ref_type`, `escalation_reason`, `last_sent_at` (`ticket-lifecycle.md`). The sweeper never cancels `CONFIRM_PENDING` (that bet is accepted locally). The lifecycle diagram's "any non-terminal state -> CANCEL_PENDING" is too broad and must be narrowed in the transition table.

For stage 3:
- `Ticket` needs `stake`, `currency`, `selections` (one per bet under B-05) and a pseudonymous `customer_ref` from the bet. Decide where that snapshot is built (when the ticket is created, stored on the ticket row, so a retransmission sends identical content).
- `Clock` appears in both `ports.py` and `runtime/clock.py` in `PROJECT_STRUCTURE.md`. Per "Decided" there is no clock port: drop it from `ports.py` when stage 3 creates the file.

For stage 4:
- Open question for the gate: D6 makes `AUTH_FAILED` fatal (the process exits). An auth outage on the provider side would then crash-loop every pod, which conflicts with the spirit of invariant 7. Alternative: long backoff, a session-status metric and an alert, with no exit. Recommendation: long backoff plus alert. A wrong credential is still visible through the metric and the logs.
- The reader catches `FrameError` and counts `kind` as the metric label. It logs `message_type` and `ticket_id` from the error.
- `encode` needs an aware `ts`: pass wall-clock UTC. It is informational only (D12).
- The fake provider needs the opposite direction (decode outbound, encode inbound). Either it builds JSON from the same `Envelope` model, or the codec gains two test-facing helpers. Decide in stage 4; do not put fake-only code in `protocol/` without saying so.
- `.env.example` and compose carry a static `PROVIDER_WS_TOKEN`; A-11 describes client credentials plus a short-lived token. That is fine for the static token provider stub. Rename the variables (prefix `RISK_`) when the integration settings are written.
- `config/settings/base.py` raises a bare `KeyError` when `DJANGO_SECRET_KEY` or `DATABASE_URL` is missing. Acceptable fail-fast; revisit with the startup probe in stage 5.

Proposed gotchas (not yet in `gotchas.md`, need approval):
- The edit hook runs `ruff check --fix`, which deletes an import that is not used yet. Add an import in the same edit as its first use.
- A `run_db` semaphore permit must be released when the worker thread finishes, not when the awaiting coroutine times out; otherwise more threads than permits are busy and the admission bound is false.
- `str(Decimal)` and pydantic's default JSON serialization can produce exponent notation (`1E+2`). Serialize wire decimals with `format(value, "f")` (done in `protocol/messages.py`).
- pydantic does not re-validate a nested model instance by default, so an instance made with `model_construct` passes into a parent unchecked. Never use `model_construct` for outbound messages.
