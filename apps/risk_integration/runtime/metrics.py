"""Metrics of the runtime loops. Labels are bounded enums only, never a ticket or bet id."""

from prometheus_client import Counter, Gauge, Histogram

FRAMES_DROPPED = Counter("risk_frames_dropped_total", "Inbound frames that could not be decoded", ["kind"])
INBOUND_IGNORED = Counter(
    "risk_inbound_ignored_total",
    "Inbound messages that changed nothing: duplicate, late, unknown ticket or uncorrelated",
    ["message", "reason"],
)
DECISIONS = Counter("risk_decisions_total", "Recommendations applied, by resulting state", ["state"])
APPLY_FAILURES = Counter("risk_apply_failures_total", "Recommendations whose unit of work failed", ["kind"])
CANCELLATIONS = Counter("risk_cancellations_total", "Tickets compensated and cancelled", ["reason"])
CANCELLATIONS_MISSED = Counter(
    "risk_cancellations_missed_total", "Cancellations that found the ticket no longer waiting", ["trigger"]
)
COMPENSATION_FAILURES = Counter(
    "risk_compensation_failures_total", "Compensations that failed; the ticket stays as it is", ["trigger", "kind"]
)
ESCALATIONS = Counter("risk_escalations_total", "Tickets escalated to risk operations", ["reason"])
ESCALATION_FAILURES = Counter("risk_escalation_failures_total", "Escalations lost to a database error", ["kind"])
MESSAGES_SENT = Counter("risk_messages_sent_total", "Frames written to the provider", ["type", "attempt"])
UNEXPECTED_ERRORS = Counter(
    "risk_unexpected_errors_total", "Unclassified errors (bugs) contained to one message or ticket", ["loop"]
)
DB_ERRORS = Counter("risk_loop_db_errors_total", "Transient database errors in a loop pass", ["loop", "kind"])
SESSIONS_ENDED = Counter(
    "risk_sessions_ended_total",
    "Provider sessions that ended or were refused; inbound_queue_full: the appliers had stopped the reader",
    ["outcome", "inbound_queue_full"],
)
SESSION_OPEN = Gauge("risk_session_open", "1 while this process holds a provider session")
TIME_TO_DECISION = Histogram(
    "risk_time_to_decision_seconds",
    "Ticket age when its recommendation was applied: how long the customer waited",
    ["decision"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15),
)
INBOUND_QUEUE_DEPTH = Gauge("risk_inbound_queue_depth", "Decoded messages waiting for an applier")
