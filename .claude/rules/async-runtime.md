---
paths:
  - "apps/risk_integration/runtime/**"
  - "apps/risk_integration/transport/**"
---

# Async runtime and transport

- Every network and DB wait has an explicit timeout. No unbounded waits except the ones a loop parks on (an empty queue, an open session).
- Only the relay sends on the socket, and it polls; there is no in-process wakeup. Appliers and the sweeper never send, and they do not retry inline: a transient failure leaves the ticket as it is, for the retransmission or the deadline. The one exception is recording an escalation, retried a few times in place.
- The relay lives inside each session's group. Every session starts with the re-send: `TICKET` for every `SENT` ticket, then each final message sent within the loss window, at most once per ticket (`final_resends`). It releases the leases of its current batch when its session ends.
- Liveness: a loop beats on every iteration, also when its DB call failed, and passes its planned wait as `extra`. A loop that waits on something that may last forever calls `park()`. The reader has no heartbeat; the keepalive covers it.
- Shutdown belongs to the connection task: stop the session group, `queue.join()`, `Relay.flush` (final messages only, never `TICKET`), close with 1001, raise `Drained`. Never wait for recommendations of `SENT` tickets.
- Session outcomes are `AUTH_FAILED`, `DISPLACED`, `ALREADY_CONNECTED`, `PLANNED`, `TRANSIENT`; the delay per outcome lives in `backoff.py`. A planned close reconnects at once only after a stable session. `AUTH_FAILED` backs off and alerts; it never exits the process.
- The transport turns every library exception into `SessionEnded(outcome)`; nothing above it imports `websockets`. A new token is fetched, with a timeout, before every connect.
- Keep the event loop free: no blocking calls, no CPU-heavy work, no sync sleeps.
