---
paths:
  - "apps/risk_integration/runtime/**"
  - "apps/risk_integration/transport/**"
---

# Async runtime and transport

- Never call the Django ORM here. All database work goes through `run_db` into persistence functions.
- All loops run under one `TaskGroup`. A task that fails or returns ends the process non-zero; do not catch and continue silently. Never swallow `CancelledError`.
- Every network and DB operation has an explicit timeout. No unbounded waits. A timed-out `run_db` means "outcome unknown": never act as if it failed; re-read state on the next pass (D5).
- The reader only decodes frames and puts them into a bounded queue; N appliers do the DB work. Only the relay sends on the socket; appliers nudge it after a commit.
- The relay retransmits `TICKET` at the retransmit point and for every `SENT` ticket after each (re)connect (D11).
- Every loop bumps its heartbeat on every iteration, including iterations whose DB call failed.
- Keep the event loop free: no blocking calls, no CPU-heavy work, no sync sleeps.
- Shutdown: stop claiming `PENDING_SEND`, let `SENT` tickets finish within the drain budget, flush `*_PENDING`, close with 1001 (D8).
- The access token comes from a token provider before every connect (A-11); a planned provider close (A-14) reconnects at once.
- Classify connection outcomes into `AUTH_FAILED`, `DISPLACED`, `ALREADY_CONNECTED`, `TRANSIENT`. Reconnect with backoff and jitter. `DISPLACED` never reconnects immediately.
- Every log line carries `ticket_id`, `bet_id` and `connection_id` where they exist. Count every ignored duplicate, late or unknown message in a metric.
