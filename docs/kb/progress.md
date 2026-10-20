# Progress and handoff

Written by the agent at the end of every stage and read at the start of the next one (sessions do not share context). Keep it short and factual. Folded into the README or removed in stage 6.

## Status

| Stage | Branch | State | Merged into main |
|-------|--------|-------|------------------|
| 0. Bootstrap | `stage/0-bootstrap` | done; scaffold and stage 0 docs in one commit by the user | yes |
| 1. Provider contract | `stage/1-protocol` | done, awaiting gate | no |
| 2. Domain and persistence | `stage/2-domain-persistence` | not started | no |
| 3. Monolith side | `stage/3-monolith` | not started | no |
| 4. Runtime core and failure scenarios | `stage/4-runtime` | not started | no |
| 5. Operability | `stage/5-ops` | not started | no |
| 6. Docs and polish | `stage/6-docs` | not started | no |

## Latest summary

Stage 1 (provider contract).

- Done:
  - `docs/PROVIDER_QUESTIONS.md`: every A-xx entry written out as a question to the provider, with the assumed answer, why it matters and what changes if the answer differs, grouped by impact (correctness, timing, sessions, wire format, capacity). B-xx entries are in a separate section.
  - `protocol/` package (pydantic + stdlib):
    - `envelope.py` with an optional `ticket_id` and optional `ts`.
    - `messages.py`: frozen models. Outbound models forbid extra fields; inbound models ignore unknown fields.
    - `codec.py`: `encode(msg, *, ts) -> str`; `decode(frame)` raises only `FrameError` with a bounded `kind` (`binary`, `not_json`, `bad_envelope`, `unknown_type`, `bad_payload`), plus type and `ticket_id` when readable; input values never go into the error detail.
    - `close_codes.py`: `classify_close(code | None)` and `classify_handshake_rejection(status)` return `AUTH_FAILED / DISPLACED / ALREADY_CONNECTED / PLANNED / TRANSIENT`. Unknown values are transient.
  - 71 unit tests (62 after the gate revision): exact decimals, envelope precedence, tolerant decoding, every malformed-frame kind, no input echo, the classification table including kick-out versus reject-new. `make check` green.
- Assumptions added: A-16 (`TICKET` payload, decline and cancellation reason enums), A-17 (HTTP statuses and close codes for session outcomes), B-05 (dropped at the gate revision). The A-02 entry now names `UNKNOWN_TICKET` as the only error code that changes our reaction; other codes are kept raw.
- Deviations / scaffold fixes:
  - Added `apps/__init__.py`. Without it, mypy saw the same file under two module names once modules imported each other.
  - `PROJECT_STRUCTURE.md`: inbound `Error` is named `ProviderError`, and `close_codes.py` adds `PLANNED` (A-14).
- How to review:
  - Read `docs/PROVIDER_QUESTIONS.md` (A-16 and A-17 are new).
  - Then `apps/risk_integration/protocol/` (about 250 lines) and the two test files `tests/test_protocol_*.py`.
  - `git log main..stage/1-protocol` shows the commits.

## Stage 1 gate revision (scope cuts, agreed with the user)

Applied in the working tree of `stage/1-protocol`, uncommitted, after the stage 1 review:
- B-01 default: a refused or late confirm goes to `ESCALATED`; no automatic void or refund. Compensation only releases a held stake from `PENDING_SEND`/`SENT`. `CancellationReason.CONFIRM_REJECTED` removed from `protocol/messages.py` (it would be dead code).
- Apply failures have three classes: domain rejection -> `DECLINE_PENDING`; transient failure or unknown outcome -> ticket stays `SENT` (retransmission, then the deadline); recognized permanent technical failure -> compensate + `CANCEL_PENDING(APPLY_FAILED)`. No inline applier retries (D4).
- Final messages: after a reconnect, every final message sent within the loss window is re-sent, whatever its type (D11). A-03 now states that a duplicate confirm after the confirm window is a no-op.
- New in the transition table (found while writing it): a reply can beat the relay's commit of its own send, so reply-driven transitions also accept the pre-send state (`PENDING_SEND`, `CONFIRM_PENDING`, `CANCEL_PENDING`), and every transition clears the lease. Without this a fast `RECOMMENDATION` is dropped until the retransmit point, and a fast refusal of a confirm goes unnoticed.
- Dropped: event publisher stub (D1), in-process relay wakeup (D2), lease requeue step (D3), `attempts`/`next_attempt_at` (stuck `*_PENDING` is an age metric with an alert), waiting for `SENT` recommendations during drain (D8), two-instance handover test, cross-process e2e suite and the `e2e` marker, PDB and ServiceMonitor, wallet ledger (one `WalletHold` per bet).
- Process: former stage 6 folded into stage 4; operability is stage 5, docs stage 6. No separate ADR files: `decisions.md` is the single record, promoted to `docs/DECISIONS.md` in stage 6; code comments cite `# D5`. `docs/adr/` removed. The time budget line was removed from `stages.md` (private context).
- Full transition table: `ticket-lifecycle.md`.
- `TICKET` payload flattened to one single bet `{stake, currency, odds, selection_id, customer_ref}` (A-16). Removed `Selection`, the selections list, business-rule validation on the payload (it belongs to the monolith; only non-finite decimals and unknown fields are refused), the fixed-point decimal serializer (values come from a DB decimal column, which never yields exponent notation) and B-05. Reason: nothing in our flow reads the payload back. Tests: 71 -> 62.

Proposed commits (after review):
1. `docs(process): review before commit, merged stages, no ADR files` — `CLAUDE.md`, `.claude/commands/stage.md`, `docs/kb/stages.md`, `docs/kb/progress.md`, `docs/adr/.gitkeep` (deleted), `Makefile`, `docker-compose.yml`, `pyproject.toml`
2. `docs: stage 1 gate scope revision (B-01 escalates, apply-failure classes, uniform re-send, single-bet payload)` — `docs/kb/decisions.md`, `docs/kb/ticket-lifecycle.md`, `docs/kb/provider-assumptions.md`, `docs/PROVIDER_QUESTIONS.md`, `docs/kb/design-topics.md`, `docs/PROJECT_STRUCTURE.md`, `.claude/rules/*.md`
3. `refactor(protocol): single-bet flat TICKET payload, drop CONFIRM_REJECTED cancellation reason` — `apps/risk_integration/protocol/messages.py`, `apps/risk_integration/tests/test_protocol_codec.py`

## Carry-over for the next stage

Open questions, deviations to remember, assumptions added (IDs), proposed gotchas not yet moved to `gotchas.md`.

Process change after stage 1 (requested by the user): from stage 2 on, nothing is staged or committed during a stage. The summary ends with "proposed commits" (conventional messages, each with its files); the user reviews the full uncommitted diff, and commits are made only after that review (`CLAUDE.md`, "Review before commit"). Stage 1 was committed under the previous flow.

For stage 2:
- Persist the cancellation reason on the ticket: `RequestCancellation.reason` is required. A re-send after a reconnect (D11) must carry the same reason, and it must come from a column, not from memory. Mapping: sweeper at the deadline gives `DEADLINE_EXCEEDED`; a recognized permanent technical apply failure gives `APPLY_FAILED`; `ERROR` about `TICKET` gives `TICKET_REJECTED`. `escalation_reason`: `CONFIRM_REJECTED`, `CANCEL_REJECTED`.
- `last_error_code` stores `ProviderError.code` raw. Treat a cancellation as done when `ProviderError.is_unknown_ticket` is true (A-09).
- Implement the transition table in `ticket-lifecycle.md` (rows 1-15 plus the ignored combinations) as the allowed/forbidden table test. Fields are listed there. Decide in the design note which concrete exceptions count as a "recognized permanent technical failure" (row 8) versus transient.

For stage 3:
- `Ticket` needs `stake`, `currency`, `odds`, `selection_id` and a pseudonymous `customer_ref` from the bet (A-16, one single bet). Decide where that snapshot is built (when the ticket is created, stored on the ticket row, so a retransmission sends identical content).
- `Clock` appears in both `ports.py` and `runtime/clock.py` in `PROJECT_STRUCTURE.md`. Per "Decided" there is no clock port: drop it from `ports.py` when stage 3 creates the file.

For stage 4:
- Open question for the gate: D6 makes `AUTH_FAILED` fatal (the process exits). An auth outage on the provider side would then crash-loop every pod, which conflicts with the spirit of invariant 7. Alternative: long backoff, a session-status metric and an alert, with no exit. Recommendation: long backoff plus alert. A wrong credential is still visible through the metric and the logs.
- The reader catches `FrameError` and counts `kind` as the metric label. It logs `message_type` and `ticket_id` from the error.
- `encode` needs an aware `ts`: pass wall-clock UTC. It is informational only (D12).
- Open point for the design note: the loss-window re-send is anchored at the disconnect time, which a freshly started process does not know (the previous pod crashed). Candidate: anchor on `max(last_sent_at)` of closed tickets at the first connect of a process.
- The fake provider needs the opposite direction (decode outbound, encode inbound). Either it builds JSON from the same `Envelope` model, or the codec gains two test-facing helpers. Decide in stage 4; do not put fake-only code in `protocol/` without saying so.
- `.env.example` and compose carry a static `PROVIDER_WS_TOKEN`; A-11 describes client credentials plus a short-lived token. That is fine for the static token provider stub. Rename the variables (prefix `RISK_`) when the integration settings are written.
- `config/settings/base.py` raises a bare `KeyError` when `DJANGO_SECRET_KEY` or `DATABASE_URL` is missing. Acceptable fail-fast; revisit with the startup probe in stage 5.

Proposed gotchas (not yet in `gotchas.md`, need approval):
- The edit hook runs `ruff check --fix`, which deletes an import that is not used yet. Add an import in the same edit as its first use.
- A `run_db` semaphore permit must be released when the worker thread finishes, not when the awaiting coroutine times out; otherwise more threads than permits are busy and the admission bound is false.
