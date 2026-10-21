"""Counters of the runtime loops. Labels are bounded enums only, never a ticket or bet id."""

from prometheus_client import Counter

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
SENDS_NOT_RECORDED = Counter(
    "risk_sends_not_recorded_total", "Sends whose transition missed: a reply came first, or the lease was lost"
)
DB_ERRORS = Counter("risk_loop_db_errors_total", "Transient database errors in a loop pass", ["loop", "kind"])
SESSIONS_ENDED = Counter("risk_sessions_ended_total", "Provider sessions that ended or were refused", ["outcome"])
