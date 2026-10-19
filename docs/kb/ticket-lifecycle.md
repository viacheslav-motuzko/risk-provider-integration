# Ticket lifecycle (draft; refine in stage 2)

```
PENDING_SEND -> SENT -> CONFIRM_PENDING -> CONFIRMED
                     -> DECLINE_PENDING -> DECLINED
any non-terminal state -> CANCEL_PENDING -> CANCELLED
cancellation rejected / exhausted retries / needs a human -> ESCALATED
```

- `PENDING_SEND` is created by the monolith in the same transaction as the bet, with `deadline_at` from the bet type (D11). The relay claims it (lease), sends `TICKET`, then sets `SENT`. A crash between the send and the state update causes a re-send, which relies on A-03.
- Retransmission (D11): a `SENT` ticket without a recommendation gets one more `TICKET` at its retransmit point, and every `SENT` ticket gets one after each (re)connect (A-07).
- On a recommendation, one transaction applies the monolith effects (bet status, wallet) and moves `SENT` to `CONFIRM_PENDING` or `DECLINE_PENDING` with a compare-and-swap on the expected state. After commit the relay sends `CONFIRM_RECOMMENDATION` or `DECLINE_RECOMMENDATION` and moves to the terminal state.
- An `ACCEPT` recommendation can still end in `DECLINE_PENDING` when our own business rules reject the bet (A-08). A permanent error while applying it (a domain rule, not a transient DB error) also goes straight to `DECLINE_PENDING` with a reason; it does not wait for the deadline.
- A transient DB failure while applying a recommendation: bounded retries within the deadline; otherwise the ticket stays `SENT` and the sweeper cancels it at the deadline. A timed-out or ambiguous apply is "outcome unknown" (D5): the next pass re-reads the ticket before doing anything.
- The sweeper moves tickets still `PENDING_SEND` or `SENT` after `deadline_at` to `CANCEL_PENDING`, with compensation in the same transaction (D7). A recommendation arriving afterwards is ignored and counted.
- A crash between apply and send is recovered by the relay finding `*_PENDING` rows.
- Final messages have no ack (A-02): after a successful send the ticket moves to its closed state (`CONFIRMED`, `DECLINED`, `CANCELLED`) and records `last_sent_at`. After a reconnect, the relay re-sends final messages sent within the loss window (D11).
- An `ERROR` is correlated by `ticket_id` and `ref_type` (A-02) and may correct a closed state. Each row is a compare-and-swap:

  | Ticket state | `ERROR` about | Result |
  |---|---|---|
  | `SENT` | TICKET | compensate, `CANCEL_PENDING` |
  | `CONFIRMED` | CONFIRM (late or rejected) | compensate, `CANCEL_PENDING` (A-10, B-01) |
  | `DECLINED` | DECLINE | log and count only; the provider treats the ticket as not accepted (A-07) |
  | `CANCELLED` | CANCEL, rejected | `ESCALATED` (A-09, B-03) |
  | `CANCELLED` | CANCEL, unknown ticket | no-op, counts as done (A-09) |

  Any other combination is ignored, logged and counted.
- Persisted for restart and reconciliation: `recommendation` (ACCEPT or DECLINE as received), `last_error_code`, `last_error_ref_type`, `escalation_reason`, `last_sent_at`.
- Undecodable frames and messages for unknown tickets are counted and logged; they never crash the reader.
- Retries on outbound sends use `attempts` and `next_attempt_at` with backoff; beyond a limit the ticket goes to `ESCALATED` and raises a metric and an alert-worthy log.
- Expired leases (a crashed worker) are returned to claimable by the sweeper; transitions still go through compare-and-swap, so a late original owner cannot overwrite a newer state.
- Indexes: partial indexes on the non-terminal states serve the claim, retransmit and sweep queries, so the table can grow without slowing them. Retention: B-04.

Refine in stage 2: the exact transition table, which fields are written on each transition (`retransmit_at`, `attempts`, `next_attempt_at`, `escalation_reason`), and the index definitions.
