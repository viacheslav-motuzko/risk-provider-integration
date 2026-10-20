# Declared assumptions and open questions (seed for `docs/PROVIDER_QUESTIONS.md`)

The task description leaves the provider side open and the provider cannot be asked live. For each row write the question to the provider (as a message to a real counterpart) and keep the assumed answer. IDs are referenced from code comments (`# ASSUMPTION A-07`).

Expanded in stage 1 into `docs/PROVIDER_QUESTIONS.md` (question text, assumed answer, why it matters, what changes if the answer differs, grouped by impact). This table stays the short index. The central gap: the task lists the four messages the provider receives, and says nothing about the message that carries its recommendation, nor about acks, errors, auth or heartbeats.

## Provider assumptions (A-xx)

| ID | Topic | Default assumed answer |
|----|-------|------------------------|
| A-01 | Inbound recommendation message | One inbound type `RECOMMENDATION {ticket_id, decision: ACCEPT or DECLINE, reason?}` |
| A-02 | Acks and errors | No application-level ack for TICKET, CONFIRM or DECLINE. An inbound `ERROR {ticket_id?, ref_type?, code, message}` exists, where `ref_type` names the type of our message it rejects, so an error can be correlated after the ticket has moved on. A cancellation can be rejected (for example too late, or the bet is already settled); the rejection arrives as `ERROR` with a distinguishable code. No reply to a cancellation means it was accepted |
| A-03 | Idempotency | Every outbound message is idempotent by `ticket_id` (+type). A re-sent TICKET with a known id is a retransmission, and the provider re-emits its original recommendation. Duplicate CONFIRM, DECLINE and CANCELLATION are no-ops. Declared simplification: avoids a second state store on our side for what the provider has already seen |
| A-04 | Timing | One ticket type: the task defines none, so recommendation time does not vary per ticket (declared simplification). The recommendation usually arrives within about 2 s and at most within about 5 s. Our deadline is one configuration value (default 10 s), stored per ticket as `deadline_at` at creation; the retransmit point is half of it. The provider accepts CONFIRM or DECLINE within 10 s after its recommendation; later ones get `ERROR` |
| A-05 | Sessions | One active session per account. A second connection is rejected (reject-new) with a distinguishable close code. A dead session is evicted after missed pings (about 30 s). If the provider allows several sessions, several instances may run in parallel; correctness does not depend on it (D3, D6, A-15) |
| A-06 | Liveness | WebSocket ping/pong every 20 s, no application heartbeat |
| A-07 | Reconnect and disconnect | No replay of messages the provider sent while we were disconnected. After a reconnect we retransmit TICKET for every ticket without a recommendation, and the provider answers with the original recommendation (A-03). No cancel-on-disconnect: the provider keeps in-flight tickets and treats unanswered ones as not accepted after its own timeout |
| A-08 | Decline after accept | Allowed (our business rejection: limits, balance). `DECLINE_RECOMMENDATION` carries an optional `reason` from a small enum |
| A-09 | Cancellation validity | Valid any time before settlement, including after CONFIRM; idempotent. For a `ticket_id` the provider never received, it is a no-op or an `ERROR` with an unknown-ticket code, which we treat as done |
| A-10 | CONFIRM rejected or late | `ERROR` in reply. Our reaction (compensate and cancel) is a business decision: B-01 |
| A-11 | Envelope and auth | JSON text frames `{type, ticket_id, ts, payload}`. A short-lived bearer token (tens of minutes) is obtained from an auth endpoint with client credentials before every connect and sent in the handshake header; there is no in-band refresh. A sandbox endpoint exists |
| A-12 | Limits | No provider rate limit below tens of messages per second per account |
| A-13 | Ordering | At most one `RECOMMENDATION` per ticket transmission; no ordering across tickets; duplicates are possible |
| A-14 | Connection lifetime | The provider closes a connection after a maximum lifetime (a few hours) with a distinguishable close code. We reconnect at once with a fresh token; this is routine, not a failure |
| A-15 | Message scope | Messages are scoped to the account, not the session: CONFIRM, DECLINE and CANCELLATION for a ticket are accepted on any session of the account. A recommendation goes to the session that sent the latest TICKET for that ticket; if that session is gone, it is lost, and our retransmission recovers it (A-03, D11) |
| A-16 | `TICKET` payload (stage 1) | `{stake, currency, selections: [{event_id, market, outcome, odds}], customer_ref}`; stake and odds as fixed-point decimal strings, stake in major units, ISO 4217 currency, pseudonymous customer reference, provider feed IDs, no ticket type. `DECLINE_RECOMMENDATION.reason?` and `REQUEST_CANCELLATION.reason` from small enums |
| A-17 | Session outcome signals (stage 1) | Upgrade refused with 401/403 (auth) or 409 (already connected). Close codes after the handshake: 4000 planned (A-14), 4001 auth, 4002 displaced, 4003 already connected; everything else transient. The token is checked only at the handshake |

When a stage needs a new assumption, add a row with the next ID here and in `docs/PROVIDER_QUESTIONS.md`, and mention it in the stage summary.

## Questions to the business (B-xx)

Some gaps are not provider questions: they decide what happens to a customer's bet or money, and the product owner or the risk/trading team decides them. Each row is a question with the default we implement. They go into `docs/PROVIDER_QUESTIONS.md` as a separate section and are referenced from code as `# QUESTION B-01`.

| ID | Question | Default we implement |
|----|----------|----------------------|
| B-01 | CONFIRM was rejected or arrived late after we had already accepted the bet. Do we void a bet the customer may have seen as accepted, or keep it with exposure the provider does not count? | Void and refund, notify the customer, raise a metric and an alert |
| B-02 | What does the customer see while a ticket waits for the provider, and what happens at the deadline? Is the deadline value acceptable? | The bet is shown as pending; at the deadline it is rejected and the stake released ("please try again"). Deadline as in A-04 |
| B-03 | The provider rejected our cancellation of a bet we voided. Who reconciles the provider's exposure, and how? | `ESCALATED`, alert to risk operations, manual reconciliation |
| B-04 | How long are ticket rows kept? Do regulatory retention rules for bet records apply to them? | Kept as long as the bet records; archiving or partitioning is a next step |
| B-05 | Single bets only, or also multiples (accumulators, system bets)? (stage 1) | Singles only in this scope; the wire format carries a list of selections, so multiples change only the monolith's bet model |

## Environment assumptions (E-xx)

Nothing is known about the real monolith or the platform it runs on, so these are declared too. They are listed in the README next to the provider assumptions and referenced from code as `# ASSUMPTION E-xx`. No question to a counterpart is needed; each row says what changes if the assumption is wrong.

| ID | Topic | Assumption and consequence if wrong |
|----|-------|--------------------------------------|
| E-01 | Python | 3.12. The effective floor is 3.11 (`asyncio.TaskGroup`, `asyncio.timeout`); older monoliths cannot host this process without an upgrade |
| E-02 | Django | 5.2 LTS. Code uses stable ORM APIs only; 4.2 would need a check of `select_for_update(skip_locked=...)` and psycopg 3 support |
| E-03 | Postgres driver | psycopg 3. The monolith's driver is unknown. All integration DB access goes through the Django ORM, so psycopg2 would be a dependency swap |
| E-04 | Postgres version | 14 or newer (`SKIP LOCKED`, `idle_in_transaction_session_timeout`); developed against 16 |
| E-05 | Database topology | `RiskTicket` lives in the monolith's database, so the ticket is created in the same transaction as the bet. One database, no cross-database transactions. If the sports domain is extracted, this becomes an outbox plus idempotent consumption |
| E-06 | Pooling | Connections may pass through a transaction-mode pooler. Nothing session-level is used: no advisory locks, no `LISTEN/NOTIFY`, no session `SET` (only `SET LOCAL`) |
| E-07 | Platform | Kubernetes with Prometheus-style scraping; secrets delivered as env vars from a Secret (D10) |
| E-08 | Time | UTC with `USE_TZ=True`. Cross-pod time comparisons use DB time (D12), so pod clock skew does not affect correctness |
| E-09 | Monolith operations | The monolith exposes, or can be given, idempotent per-bet operations: hold/capture/release for wallets and accept/reject/void for bets. Here they are stubs behind `BetDecisionPort` |
| E-10 | Database failover | A failover to an asynchronous replica can lose transactions that were committed and whose messages were already sent. Recovery then relies on A-03 and E-09; reconciliation beyond that is out of scope and stated in the README |
| E-11 | Migrations | Applied by the monolith's release pipeline before the rollout of either Deployment. The integration process does not migrate; its startup probe stays red while migrations are pending |
