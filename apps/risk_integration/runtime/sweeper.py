"""Deadlines: a ticket still waiting for its recommendation at `deadline_at` (DB time) is compensated and
cancelled. The cancellation re-checks the deadline and the state under the ticket lock, so a recommendation
that wins the race is applied and the cancellation misses."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import NoReturn
from uuid import UUID

import structlog

from apps.risk_integration.domain.machine import Trigger
from apps.risk_integration.persistence import tickets
from apps.risk_integration.persistence.db import DbRunner
from apps.risk_integration.persistence.errors import TRANSIENT_DB_ERRORS, transient_kind
from apps.risk_integration.runtime import metrics
from apps.risk_integration.runtime.decisions import Decisions

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SweeperSettings:
    interval: timedelta
    batch_size: int
    retry_failed_after: timedelta


class Sweeper:
    def __init__(self, run_db: DbRunner, decisions: Decisions, settings: SweeperSettings) -> None:
        self._run_db = run_db
        self._decisions = decisions
        self._settings = settings
        # In memory only: a restart forgets it and retries every failed ticket at once, which is harmless.
        self._failed_until: dict[UUID, float] = {}

    async def run(self) -> NoReturn:
        while True:
            try:
                expired = await self._run_db(
                    tickets.find_expired, limit=self._settings.batch_size, exclude=self._recently_failed()
                )
            except TRANSIENT_DB_ERRORS as exc:
                kind = transient_kind(exc)
                log.warning("sweeper_db_error", kind=kind, error=type(exc).__name__)
                metrics.DB_ERRORS.labels(loop="sweeper", kind=kind).inc()
                expired = []
            for ticket_id in expired:
                if not await self._decisions.compensate(ticket_id, Trigger.DEADLINE_PASSED):
                    self._failed_until[ticket_id] = self._now() + self._settings.retry_failed_after.total_seconds()
            # Always pause: a full batch of tickets whose compensation keeps failing would otherwise spin.
            await asyncio.sleep(self._settings.interval.total_seconds())

    def _recently_failed(self) -> list[UUID]:
        """Tickets whose compensation failed lately, left out of the next batches so that a batch full of them
        cannot starve the tickets behind it. Bounded by batch size times retry_failed_after / interval."""
        now = self._now()
        self._failed_until = {t: until for t, until in self._failed_until.items() if until > now}
        return list(self._failed_until)

    @staticmethod
    def _now() -> float:
        # A local skip, not a cross-pod comparison: the loop's monotonic clock is enough.
        return asyncio.get_running_loop().time()
