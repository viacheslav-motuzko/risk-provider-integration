"""The use cases as the loops call them: each is one `run_db` unit, its failure is classified, logged and counted.
A transient failure leaves the ticket as it is, for the retransmission or the deadline; an unclassified one is a
bug and propagates to the loop, which contains it to that one message or ticket. The one exception is an
escalation, retried a few times: nothing brings a provider error back."""

import asyncio
from datetime import timedelta
from uuid import UUID

import structlog
from structlog.contextvars import bound_contextvars

from apps.risk_integration import services
from apps.risk_integration.domain.machine import CANCEL_REASONS, ESCALATION_REASONS, Trigger
from apps.risk_integration.domain.states import Decision
from apps.risk_integration.persistence import tickets
from apps.risk_integration.persistence.db import DbRunner, OutcomeUnknown
from apps.risk_integration.persistence.errors import (
    TRANSIENT_DB_ERRORS,
    ApplyFailure,
    classify_apply_failure,
    transient_kind,
)
from apps.risk_integration.ports import BetDecisionPort
from apps.risk_integration.runtime import metrics

log = structlog.get_logger(__name__)


class Decisions:
    def __init__(
        self,
        run_db: DbRunner,
        port: BetDecisionPort,
        *,
        escalation_attempts: int = 3,
        escalation_retry_delay: timedelta = timedelta(milliseconds=500),
    ) -> None:
        self._run_db = run_db
        self._port = port
        self._escalation_attempts = escalation_attempts
        self._escalation_retry_delay = escalation_retry_delay.total_seconds()

    async def apply(self, ticket_id: UUID, decision: Decision) -> None:
        with bound_contextvars(ticket_id=str(ticket_id)):
            try:
                applied = await self._run_db(services.apply_recommendation, self._port, ticket_id, decision)
            except Exception as exc:
                failure = classify_apply_failure(exc)
                if failure is None:
                    raise
                if failure is ApplyFailure.TRANSIENT:
                    kind = transient_kind(exc)
                    log.warning("apply_failed", decision=decision, kind=kind, error=type(exc).__name__)
                    metrics.APPLY_FAILURES.labels(kind=kind).inc()
                    return
                log.error("apply_failed", decision=decision, kind="permanent", error=type(exc).__name__)
                metrics.APPLY_FAILURES.labels(kind="permanent").inc()
                await self.compensate(ticket_id, Trigger.APPLY_FAILED)
                return
            if isinstance(applied, services.Ignored):
                log.info("recommendation_ignored", decision=decision, reason=applied)
                metrics.INBOUND_IGNORED.labels(message="RECOMMENDATION", reason=applied).inc()
                return
            waited = applied.waited.total_seconds()
            log.info("recommendation_applied", decision=decision, state=applied.state, waited_seconds=round(waited, 3))
            metrics.DECISIONS.labels(state=applied.state).inc()
            metrics.TIME_TO_DECISION.labels(decision=decision).observe(waited)

    async def compensate(self, ticket_id: UUID, trigger: Trigger, *, error_code: str = "") -> bool:
        """Void the bet, release the stake and set `CANCEL_PENDING`, if the ticket still waits. False when the
        compensation failed, so the ticket still waits."""
        with bound_contextvars(ticket_id=str(ticket_id)):
            try:
                cancelled = await self._run_db(
                    services.cancel_ticket, self._port, ticket_id, trigger, error_code=error_code
                )
            except Exception as exc:
                failure = classify_apply_failure(exc)
                if failure is None:
                    raise
                kind = transient_kind(exc) if failure is ApplyFailure.TRANSIENT else "permanent"
                # Permanent means the monolith refused (an accepted bet behind a waiting ticket): the sweeper
                # meets the ticket again on every pass after its deadline, and a person has to resolve it.
                log.error("compensation_failed", trigger=trigger, kind=kind, error=type(exc).__name__)
                metrics.COMPENSATION_FAILURES.labels(trigger=trigger, kind=kind).inc()
                return False
            if not cancelled:
                log.info("cancellation_missed", trigger=trigger)
                metrics.CANCELLATIONS_MISSED.labels(trigger=trigger).inc()
                return True
            reason = CANCEL_REASONS[trigger]
            log.warning("ticket_cancelled", reason=reason, error_code=error_code or None)
            metrics.CANCELLATIONS.labels(reason=reason).inc()
            return True

    async def escalate(self, ticket_id: UUID, trigger: Trigger, *, error_code: str) -> None:
        """The provider refused our confirm or cancellation. No money moves."""
        with bound_contextvars(ticket_id=str(ticket_id)):
            escalated = await self._escalate_with_retries(ticket_id, trigger, error_code)
            if escalated is None:
                return
            if not escalated:
                log.info("provider_error_ignored", trigger=trigger, reason="late", error_code=error_code)
                metrics.INBOUND_IGNORED.labels(message="ERROR", reason="late").inc()
                return
            reason = ESCALATION_REASONS[trigger]
            log.error("ticket_escalated", reason=reason, error_code=error_code)
            metrics.ESCALATIONS.labels(reason=reason).inc()

    async def _escalate_with_retries(self, ticket_id: UUID, trigger: Trigger, error_code: str) -> bool | None:
        """The transition's result, or None when every attempt failed. Retried in place, unlike every other use
        case: the provider does not repeat an error, so nothing else would bring this one back. Safe to repeat,
        because the transition is conditional. A miss after an unknown outcome is checked against the ticket: it
        may be our own earlier attempt that committed, and that is an escalation, not a late error."""
        attempt = 1
        outcome_unknown = False
        while True:
            try:
                escalated: bool = await self._run_db(tickets.escalate, ticket_id, trigger, error_code=error_code)
                if not escalated and outcome_unknown:
                    escalated = await self._run_db(tickets.is_escalated, ticket_id, trigger, error_code=error_code)
            except TRANSIENT_DB_ERRORS as exc:
                outcome_unknown = outcome_unknown or isinstance(exc, OutcomeUnknown)
                kind = transient_kind(exc)
                if attempt >= self._escalation_attempts:
                    # Lost unless a re-send after a reconnect draws the same error again.
                    log.error("escalation_failed", trigger=trigger, kind=kind, error_code=error_code)
                    metrics.ESCALATION_FAILURES.labels(kind=kind).inc()
                    return None
                log.warning("escalation_retried", trigger=trigger, kind=kind, attempt=attempt)
                attempt += 1
                await asyncio.sleep(self._escalation_retry_delay)
            else:
                return escalated
