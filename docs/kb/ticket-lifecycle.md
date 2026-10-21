# Ticket lifecycle

Revised at the stage 1 gate: B-01 escalates instead of voiding, apply failures have three classes, replies may beat the relay's own commit, final messages are re-sent uniformly (D11). The transition table is implemented in `domain/machine.py` (stage 2), which adds one self-loop trigger, `FINAL_RESENT`, for the re-send of a final message after a reconnect.

```
PENDING_SEND -> SENT -> CONFIRM_PENDING -> CONFIRMED
                     -> DECLINE_PENDING -> DECLINED
PENDING_SEND | SENT -> CANCEL_PENDING -> CANCELLED          our placement failed: compensate first (D7)
CONFIRM_PENDING | CONFIRMED -> ESCALATED                    provider refused or missed our confirm (B-01)
CANCEL_PENDING | CANCELLED -> ESCALATED                     provider refused our cancellation (B-03)
```

Compensation means: void the bet and release the held stake, in the same transaction as the transition. It happens only from the pre-acceptance states (`PENDING_SEND`, `SENT`), where the stake is held and never captured. No path refunds a captured stake.

## Transition table

Every transition is a conditional update on the listed source states (plus the lease owner for the relay's send transitions), runs in one transaction together with its side effects, and clears the lease. A miss is a normal outcome: logged and counted, never an exception.

Rows 4-6 and 8-10 move money. Their unit of work locks the ticket in its source states first (which also yields `bet_id`), lets the monolith decide and apply through the port, then commits the transition (`services.py`, stage 3). A duplicate or a racing cancellation waits for the lock and then finds nothing to do.

A reply can arrive before the relay has committed the transition for the frame it just sent (the relay sends, then commits). So every reply-driven transition also accepts the state before that send: `PENDING_SEND` for replies to `TICKET`, `CONFIRM_PENDING` for replies to a confirm, `CANCEL_PENDING` for replies to a cancellation. The relay's own commit then misses, which is a normal outcome. Without this, a fast reply would be dropped as unexpected and, for a refused confirm, the exposure mismatch would go unnoticed.

| # | From | Trigger | To | Same transaction |
|---|------|---------|----|------------------|
| 1 | (new) | `place_bet` in the monolith | `PENDING_SEND` | bet `PENDING_RISK`, stake held, ticket payload snapshot, `deadline_at` (D11) |
| 2 | `PENDING_SEND` | relay sent `TICKET` | `SENT` | `last_sent_at`, `retransmit_at`. A crash before this commit causes a re-send (A-03) |
| 3 | `SENT` | relay re-sent `TICKET` at `retransmit_at`, or after a (re)connect | `SENT` | `last_sent_at`; `retransmit_at` cleared (one retransmit point per ticket; every reconnect re-sends all `SENT`, A-07) |
| 4 | `PENDING_SEND`, `SENT` | `RECOMMENDATION` ACCEPT, applied | `CONFIRM_PENDING` | bet accepted, stake captured, `recommendation` |
| 5 | `PENDING_SEND`, `SENT` | `RECOMMENDATION` DECLINE, applied | `DECLINE_PENDING` | bet rejected, stake released, `recommendation` |
| 6 | `PENDING_SEND`, `SENT` | ACCEPT, but our domain rules reject the bet (limits, A-08) | `DECLINE_PENDING` | bet rejected, stake released, `recommendation`, `decline_reason` |
| 7 | `PENDING_SEND`, `SENT` | transient failure or unknown outcome while applying (DB error, timeout, `OutcomeUnknown`) | unchanged | nothing (rolled back, or not known). No inline retry (D4): the retransmit (#3) brings the recommendation back (A-03), otherwise the deadline (#9) cancels. An unclassified exception fails the task (D4) and leaves the ticket unchanged as well |
| 8 | `PENDING_SEND`, `SENT` | recognized permanent technical failure while applying (the task's "unexpected effects") | `CANCEL_PENDING` | compensate, `cancel_reason = APPLY_FAILED`. A separate transaction after the failed one rolled back |
| 9 | `PENDING_SEND`, `SENT` | `deadline_at` passed (sweeper, DB time) | `CANCEL_PENDING` | compensate, `cancel_reason = DEADLINE_EXCEEDED`. `PENDING_SEND` too: it may have reached the provider before a crash (A-09) |
| 10 | `PENDING_SEND`, `SENT` | `ERROR` with `ref_type = TICKET` | `CANCEL_PENDING` | compensate, `cancel_reason = TICKET_REJECTED`, `last_error_code`, `last_error_ref_type` |
| 11 | `CONFIRM_PENDING` | relay sent confirm | `CONFIRMED` | `last_sent_at` |
| 12 | `DECLINE_PENDING` | relay sent decline | `DECLINED` | `last_sent_at` |
| 13 | `CANCEL_PENDING` | relay sent cancellation | `CANCELLED` | `last_sent_at` |
| 14 | `CONFIRM_PENDING`, `CONFIRMED` | `ERROR` with `ref_type = CONFIRM_RECOMMENDATION` (refused or late, A-10) | `ESCALATED` | `last_error_*`, `escalation_reason = CONFIRM_REJECTED`. The bet stays accepted: no void, no refund (B-01). Retried in place on a transient DB error, the one exception to no inline retry: the provider does not repeat an error (D4) |
| 15 | `CANCEL_PENDING`, `CANCELLED` | `ERROR` with `ref_type = REQUEST_CANCELLATION`, code other than `UNKNOWN_TICKET` | `ESCALATED` | `last_error_*`, `escalation_reason = CANCEL_REJECTED` (A-09, B-03). Retried in place like row 14 (D4) |

Ignored, logged and counted (no transition):
- A `RECOMMENDATION` in any state other than `PENDING_SEND` or `SENT`: a duplicate, or late after the deadline cancellation or another cancellation (D11). Between the deadline and the sweeper's pass the ticket is still `SENT` and the recommendation is applied.
- An `ERROR` about a decline in `DECLINE_PENDING` or `DECLINED`: the provider treats the ticket as not accepted anyway (A-07).
- An `ERROR` about a cancellation with code `UNKNOWN_TICKET`: the cancellation counts as done (A-09).
- Any other combination, an `ERROR` without `ticket_id`, and messages for unknown tickets.

The sweeper never touches `CONFIRM_PENDING` or `DECLINE_PENDING`: the decision is applied locally, and only its delivery is pending. A `*_PENDING` row that cannot be sent (no session) stays pending; the age of the oldest one is a metric with an alert. There is no attempt counter.

## Relay, leases and re-send

- The relay is the only sender. It claims `PENDING_SEND`, `SENT` rows whose `retransmit_at` has passed, and `*_PENDING` rows with `SELECT ... FOR UPDATE SKIP LOCKED` and a lease, commits, sends, then applies #2, #3 or #11-13. An expired lease is claimable by the same query; there is no separate requeue step (D3).
- After each (re)connect: re-send `TICKET` for every `SENT` ticket (#3), and re-send the final message of every `CONFIRMED`, `DECLINED` or `CANCELLED` ticket whose `last_sent_at` lies within the loss window before the disconnect, whatever its type (D11). A re-send does not change the state; the cancellation reason comes from the row, not from memory.

## Persisted fields

Beyond state and the bet reference: `created_at` (DB default), `deadline_at`, `retransmit_at`, `last_sent_at`, `locked_by`, `locked_until`, the ticket payload snapshot, `recommendation` (ACCEPT or DECLINE as received), `decline_reason`, `cancel_reason`, `last_error_code`, `last_error_ref_type`, `escalation_reason`. Partial indexes on the non-terminal states serve the claim, retransmit and deadline queries, so the table can grow without slowing them; the re-send query uses an index on `last_sent_at` for closed states. Check constraints: the state is a known value, a cancelled or cancelling ticket has a `cancel_reason`, an escalated one has an `escalation_reason`. Retention: B-04.
