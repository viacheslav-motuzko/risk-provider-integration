# Questions to the provider and to the business

The task specifies the four messages we send to the risk provider (`TICKET`, `CONFIRM_RECOMMENDATION`, `DECLINE_RECOMMENDATION`, `REQUEST_CANCELLATION`). It specifies nothing the provider sends back: not the recommendation itself, not acks, errors, authentication, heartbeats or session rules. The provider cannot be asked during this exercise, so every gap below is written as the question we would send, with the answer we assume and implement.

Each entry has:
- **Q**: the question as sent to the provider's integration contact.
- **Assumed**: the answer the code is built on.
- **Why it matters**: what goes wrong if we guess wrong.
- **If different**: what changes in the code. Wire-format guesses live only in `apps/risk_integration/protocol/` (the anti-corruption layer), so most corrections stay in that package.

Code does not carry these IDs: where an assumption matters, the code states it in words, and provider guesses are marked `# ASSUMPTION:`. Environment assumptions (E-xx) are not questions to a counterpart; they are listed in the README.

## 1. Provider: correctness of the ticket flow

These answers decide whether a bet can be lost, accepted twice, or left with exposure that one side does not count.

### A-01. The recommendation message
- **Q**: Which message carries your accept or decline recommendation? What are its type name and fields? Can it carry a reason, and is the set of decisions exactly accept and decline (no "refer", no partial stake or changed odds)?
- **Assumed**: One inbound type, `RECOMMENDATION {ticket_id, decision: ACCEPT | DECLINE, reason?}`. No partial acceptance and no counter-offer.
- **Why it matters**: This is the message the whole flow waits for. A third decision (refer to a trader, reduced stake) would need new states, and a customer-facing flow for a counter-offer.
- **If different**: Rename or reshape `Recommendation` in `protocol/messages.py`. A new decision value needs a new transition in the state machine. Until then the codec reports it as `bad_payload`, so it is dropped and counted, and the ticket is cancelled at its deadline rather than misread.

### A-02. Acknowledgements and errors
- **Q**: Do you acknowledge `TICKET`, `CONFIRM_RECOMMENDATION`, `DECLINE_RECOMMENDATION` or `REQUEST_CANCELLATION` at the application level? How do you report a rejected or invalid message? Does the error say which message (type and ticket) it rejects? Can a cancellation be refused, and how do we tell a refusal from an accepted cancellation?
- **Assumed**: No application-level acks. The provider sends `ERROR {ticket_id?, ref_type?, code, message}`. `ref_type` is the type of our message it rejects, so a late error can be matched to the right step after the ticket has moved on. A refused cancellation arrives as `ERROR` with `ref_type = REQUEST_CANCELLATION` and a code other than `UNKNOWN_TICKET`. No reply to a cancellation means it was accepted. Codes we expect, for illustration: `UNKNOWN_TICKET`, `TOO_LATE`, `INVALID_MESSAGE`; only `UNKNOWN_TICKET` changes our reaction, and every code is stored as received.
- **Why it matters**: Without acks we cannot know that a final message arrived. That is why final messages are re-sent after a reconnect within a bounded window (D11), and why that remaining loss window is stated openly. Without `ref_type`, an error that arrives after a ticket's state has moved on could not be applied safely.
- **If different**: With acks, final messages would become at-least-once with retransmission until acked. That means new states (`CONFIRM_SENT`, ...) and the re-send window goes away. Without `ref_type`, errors could only be matched to the ticket's current state, and errors about earlier steps would be ignored and counted.

### A-03. Idempotency
- **Q**: Is every message idempotent by `ticket_id` and message type? If we send the same `TICKET` twice, do you treat the second as a retransmission and answer with the original recommendation, without evaluating the ticket again? Are duplicate confirms, declines and cancellations no-ops, including a duplicate confirm that arrives after your confirm window?
- **Assumed**: Yes to all. A duplicate confirm for a ticket whose confirm you already took is a no-op even after the confirm window (A-04); it does not draw a "too late" error.
- **Why it matters**: This is the foundation of our delivery model. `TICKET` is sent at least once: once more at the retransmit point, after every reconnect, and after a crash between send and commit. If a duplicate counted as a second bet, the provider would see double exposure. Final messages are also re-sent after a reconnect (D11), which relies on duplicates being no-ops.
- **If different**: We would have to record what the provider has already seen, which needs acks (A-02), and fall back to sending `TICKET` at most once. A crash or disconnect would then lose every bet in flight (D2, "considered and rejected").

### A-09. When a cancellation is valid
- **Q**: Until when can we cancel a ticket: before your recommendation, after our confirm, until the event settles? Is a repeated cancellation a no-op? What happens if we cancel a `ticket_id` you never received?
- **Assumed**: Valid any time before settlement, including after a confirm, and idempotent. A cancellation for an unknown `ticket_id` is either a no-op or an `ERROR` with code `UNKNOWN_TICKET`. Either way we treat it as done.
- **Why it matters**: When our placement flow fails, we void the bet locally first and cancel afterwards (D7). Some of those tickets may never have reached the provider (a crash before the first send). A cancellation must be safe in both cases.
- **If different**: If cancellation after a confirm is not allowed, a failure after confirming cannot be undone on the provider side. Those tickets go to `ESCALATED` for manual reconciliation (B-03). If cancelling an unknown ticket is an error with another code, add that code to the "done" list in `protocol/messages.py`.

### A-10. A rejected or late confirm
- **Q**: What happens when our `CONFIRM_RECOMMENDATION` arrives after your confirmation window, or is refused for another reason? Is the ticket then treated as not accepted on your side?
- **Assumed**: An `ERROR` with `ref_type = CONFIRM_RECOMMENDATION` (for example `TOO_LATE`). The provider treats the ticket as not accepted. Our reaction is a business decision (B-01): the ticket goes to `ESCALATED`, and the bet is not voided automatically.
- **Why it matters**: By that point we have already accepted the bet and captured the stake. If the provider does not count the bet, our exposure and theirs differ.
- **If different**: If a late confirm is still accepted, the reaction for this error becomes "log and count" and B-01 no longer applies.

### A-13. Duplicates and ordering
- **Q**: Can you send more than one recommendation for one transmission of a ticket? Is there any ordering guarantee across tickets, or between a recommendation and an error for the same ticket?
- **Assumed**: At most one recommendation per transmission of a ticket. Because we retransmit, we may receive duplicates. There is no ordering across tickets.
- **Why it matters**: The appliers process messages concurrently, and order across tickets is not preserved on our side either (D3, D4).
- **If different**: Nothing changes for duplicates: every transition is a compare-and-swap, so a duplicate is ignored and counted. If a recommendation could change for the same ticket (a revised decision), we would need a rule for which one wins. That is a business question.

## 2. Provider: timing

### A-04. Response times and windows
- **Q**: How quickly do you usually answer a ticket, and what is the maximum? Does it differ by ticket type, sport, live versus pre-match, or stake? How long after your recommendation do you accept our confirm or decline?
- **Assumed**: One ticket type, because the task defines none. The recommendation usually arrives within about 2 s and at most within about 5 s. Our deadline is one configured value (default 10 s), stored per ticket as `deadline_at` when the ticket is created. The retransmit point is half the deadline. A confirm or decline is accepted within 10 s after the recommendation.
- **Why it matters**: The deadline is how long a customer waits before the bet is rejected (B-02). The confirm window decides whether a confirm lost in a disconnect is still recovered by the re-send after the reconnect (D11); a re-send that comes too late ends in `ESCALATED` (B-01).
- **If different**: Per-type timing means `deadline_at` is computed from the type when the ticket is created. The column already exists per ticket, so only the computation changes. A shorter confirm window means more lost confirms end in `ESCALATED` instead of being recovered.

## 3. Provider: sessions and connectivity

### A-05. Session policy
- **Q**: How many concurrent WebSocket sessions may one account hold? If a second connects while the first is open, do you refuse the new one or close the old one? How is that signalled? How long do you keep a session whose client died without closing it?
- **Assumed**: One active session per account. A second connection is refused (reject-new), with a distinguishable status or close code (A-17). A dead session is dropped after missed pings, in about 30 s.
- **Why it matters**: It decides how a rolling update hands over the session, and whether more than one replica can run. Correctness does not depend on it: claims, compare-and-swap transitions and A-03 make parallel instances safe (D3, D6).
- **If different**: If the newer connection displaces the old one (kick-out), one replica still works: the new pod displaces the terminating old one. More than one replica would make pods displace each other in a loop. In that case we would add the documented leader lease row (D6). If several sessions are allowed, every instance is active, which is safe under A-15.

### A-06. Liveness
- **Q**: Do you use WebSocket ping/pong, an application-level heartbeat, or both? What interval and timeout?
- **Assumed**: Protocol-level ping/pong every 20 s. No application heartbeat.
- **Why it matters**: A half-open TCP connection looks alive until a ping times out. The ping interval plus timeout is our loss window for final messages (D11).
- **If different**: An application heartbeat would become one more inbound message type in the codec and a sender task. Today the codec reports such a frame as `unknown_type`, so it is dropped and counted, never treated as fatal.

### A-07. Reconnects and messages sent while we were disconnected
- **Q**: If our connection drops, do you replay recommendations you sent while we were away? Do you cancel tickets that were in flight on that session (cancel-on-disconnect)? How long do you keep a ticket without a confirm before you treat it as not accepted?
- **Assumed**: No replay and no cancel-on-disconnect. After a reconnect we retransmit `TICKET` for every ticket still waiting, and you answer with the original recommendation (A-03). An unconfirmed ticket is treated as not accepted after your own timeout.
- **Why it matters**: This is how in-flight tickets survive a reconnect, a crash or a rollout.
- **If different**: With replay, retransmission after a reconnect becomes redundant but harmless. With cancel-on-disconnect, tickets in flight at a disconnect are cancelled by the provider, and their recommendation never comes. Our sweeper then cancels them at the deadline (the cancellation is a no-op on your side, A-09), and the customer gets "please try again" (B-02).

### A-14. Maximum connection lifetime
- **Q**: Do you close long-lived connections on purpose (maximum lifetime, maintenance, deploys)? With which close code, and how much notice?
- **Assumed**: Yes, after a few hours, with a distinguishable close code (A-17) and no notice. We reconnect at once with a fresh token. This is routine, not an error.
- **Why it matters**: A planned close must not trigger the backoff used for failures, and must not page anyone.
- **If different**: A notice message ("closing in N seconds") would let us stop sending before the close. It would be one more inbound type plus a drain step.

### A-15. Message scope
- **Q**: Are messages scoped to the account or to the session? If we send a `TICKET` on one session and later confirm or cancel it on another, is that accepted? Which session receives the recommendation?
- **Assumed**: Account scope. Confirms, declines and cancellations are accepted on any session of the account. The recommendation goes to the session that sent the latest `TICKET` for that ticket; if that session is gone, the recommendation is lost and our retransmission recovers it.
- **Why it matters**: After a rollout or a crash, the instance that applies the decision may hold a different session than the one that sent the ticket.
- **If different**: Under session scope, a final message sent on a different session than the ticket's latest `TICKET` would be refused. Waiting tickets are already retransmitted after a reconnect. Tickets that already hold a decision (`*_PENDING`) would need a fresh `TICKET` on the new session and the recommendation again before their final message. That changes the relay and the transition table, not the protocol package.

### A-17. How session outcomes are signalled (new in stage 1)
- **Q**: Which HTTP statuses do you return when refusing the WebSocket upgrade, and which close codes do you use after the handshake? Specifically for: invalid or expired credentials, a second session for the same account, a session closed because another one took over, and a planned close.
- **Assumed**:
  - At the upgrade: `401` or `403` means authentication failed; `409` means the account already has a session.
  - After the handshake: `4000` is a planned close (maximum lifetime, A-14); `4001` is an authentication failure; `4002` means this session was displaced by a newer one; `4003` means the account already has a session.
  - Anything else, including `1000`, `1001`, `1006` (no close frame), `1011` and unknown codes, is transient: we reconnect with backoff.
  - The token is checked only at the handshake, so its expiry does not close an open session.
- **Why it matters**: Each outcome has a different reconnect policy (D6). Telling them apart is what makes the rollout handover fast and keeps two pods from fighting over the session.
- **If different**: The mapping is two dictionaries in `protocol/close_codes.py`. Unknown codes fall back to transient on purpose: retrying is always safe, while a wrong "fatal" would stop the integration.

## 4. Provider: wire format and authentication

### A-11. Envelope, transport and authentication
- **Q**: What is the frame format (JSON text, binary, field names)? How do we authenticate: static key, token, mTLS? How long does a token live, and can it be refreshed in-band? Is there a sandbox endpoint?
- **Assumed**: JSON text frames `{type, ticket_id, ts, payload}`, with `ts` in ISO 8601 UTC. A short-lived bearer token (tens of minutes) is obtained from an auth endpoint with client credentials before every connect and sent in the handshake header. There is no in-band refresh. A sandbox exists.
- **Why it matters**: The token decides how secrets are stored and rotated (D10). The envelope decides how errors are correlated.
- **If different**: The envelope is one model in `protocol/envelope.py`. A different auth scheme only changes the token provider in the transport (stage 4). We decode tolerantly: unknown fields are ignored, a missing `ts` is accepted (D12: the provider's time never decides anything), binary frames are counted and dropped.

### A-16. The `TICKET` payload (new in stage 1)
- **Q**: Which fields does `TICKET` require? How are stake, currency and odds encoded? Do you need the customer's identity or only a stable reference? How do you identify the outcome bet on: your IDs or ours? Do `DECLINE_RECOMMENDATION` and `REQUEST_CANCELLATION` take a reason?
- **Assumed**:
  - One single bet per ticket: `TICKET.payload = {stake, currency, odds, selection_id, customer_ref}`.
  - `stake` is in major currency units and `currency` is ISO 4217.
  - `stake` and `odds` (decimal odds) are decimal strings, never JSON numbers.
  - `selection_id` is the provider's outcome ID from its feed, already known to the monolith.
  - `customer_ref` is a pseudonymous stable reference, not personal data.
  - There is no ticket type.
  - `DECLINE_RECOMMENDATION` takes an optional `reason`: `INSUFFICIENT_FUNDS`, `LIMIT_EXCEEDED`, `BET_NOT_PLACEABLE` or `OTHER` (A-08).
  - `REQUEST_CANCELLATION` takes a `reason`: `DEADLINE_EXCEEDED`, `APPLY_FAILED` or `TICKET_REJECTED`. A refused confirm is not cancelled (B-01).
- **Why it matters**: Money must not pass through floats. Personal data should not leave our system without need. A reason on cancellations helps the provider and our risk team reconcile. Nothing in our flow reads the payload back: delivery, retransmission and the state machine key on `ticket_id` only, so the payload is deliberately minimal.
- **If different**: Only `Ticket` and the reason enums in `protocol/messages.py` change. Multiples (accumulators) would turn `odds` and `selection_id` into a list of legs; the integration flow does not change.

### A-08. Declining after an accept recommendation
- **Q**: May we decline a ticket you recommended to accept (for example insufficient balance or our own limits)? Do you want a reason?
- **Assumed**: Allowed. `DECLINE_RECOMMENDATION` carries an optional reason from a small enum (A-16).
- **Why it matters**: Our own business rules run after your recommendation. A bet you would accept can still be rejected by us.
- **If different**: If declining after an accept is not allowed, every business check must run before we send `TICKET`. Holding the stake when the bet is placed already covers the balance check. Limits that can only be checked at apply time would have to move earlier.

## 5. Provider: capacity

### A-12. Rate limits
- **Q**: Are there rate limits per account or per session: messages per second, connection attempts per minute?
- **Assumed**: No message limit below tens of messages per second per account. Connection attempts are limited by our own backoff with jitter.
- **Why it matters**: Retransmitting every waiting ticket after a reconnect produces a burst. Our expected load is tens of tickets per second with peaks during live events (D9).
- **If different**: Add a token bucket in the relay, the only sender. With a low connection ceiling, raise the minimum reconnect backoff.

## 6. Business questions (B-xx)

These are not provider facts. They decide what happens to a customer's bet or money, so the product owner or the risk/trading team answers them. Each has the default we implement.

### B-01. A confirm was refused or late after we accepted the bet
- **Q**: The provider refused or did not take our confirm (A-10) after we had already accepted the bet and captured the stake. Do we void a bet the customer may already have seen as accepted, or keep it with exposure the provider does not count?
- **Default**: Keep the bet. The ticket goes to `ESCALATED` with the provider's error code; risk operations get a metric and an alert and resolve it manually. No automatic void or refund: the customer may already have seen the bet as accepted, and an exposure mismatch that people resolve does less harm than reversing an accepted bet on an ambiguous signal.
- **If different**: Voiding needs a compensation path from an accepted state (refunding a captured stake, which the wallet stub does not have), a new cancellation reason, and a customer notification.

### B-02. What the customer sees while waiting, and at the deadline
- **Q**: How is a bet shown while the provider has not answered? What happens at the deadline? Is the deadline value (A-04) acceptable?
- **Default**: Shown as pending. At the deadline the bet is rejected and the stake released ("please try again"). Deadline 10 s.
- **If different**: Only configuration and the monolith's presentation change.

### B-03. The provider refused our cancellation of a bet we voided
- **Q**: Who reconciles the provider's exposure for a bet we voided locally when the provider refused the cancellation, and how?
- **Default**: The ticket goes to `ESCALATED`, risk operations get an alert, and reconciliation is manual.
- **If different**: An automated reconciliation job would read `ESCALATED` tickets. Their state and error code are already stored for it.

### B-04. Retention of ticket rows
- **Q**: How long are ticket rows kept? Do regulatory retention rules for bet records apply to them?
- **Default**: Kept as long as the bet records. Archiving or partitioning is a next step.
- **If different**: Partition by creation month. The partial indexes on non-terminal states keep the hot queries fast in any case.
