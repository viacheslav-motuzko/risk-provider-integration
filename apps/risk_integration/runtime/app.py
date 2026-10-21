"""Composition root: builds every part from the settings, serves the probes, waits until the database is ready
and runs the loops under one supervisor until a shutdown request has been drained."""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import partial

import structlog

from apps.risk_integration.adapters.monolith import DjangoBetDecisionAdapter
from apps.risk_integration.persistence import schema
from apps.risk_integration.persistence.db import DbRunner
from apps.risk_integration.persistence.errors import TRANSIENT_DB_ERRORS
from apps.risk_integration.ports import BetDecisionPort
from apps.risk_integration.runtime import metrics
from apps.risk_integration.runtime.backoff import Backoff, BackoffPolicy
from apps.risk_integration.runtime.config import IntegrationSettings
from apps.risk_integration.runtime.connection import Connection, run_connection
from apps.risk_integration.runtime.decisions import Decisions
from apps.risk_integration.runtime.health import Health, Heartbeats, serve_health
from apps.risk_integration.runtime.inbound import InboundQueue, apply_messages
from apps.risk_integration.runtime.relay import Relay, RelaySettings
from apps.risk_integration.runtime.shutdown import Drained, Shutdown
from apps.risk_integration.runtime.supervisor import supervise
from apps.risk_integration.runtime.sweeper import Sweeper, SweeperSettings
from apps.risk_integration.transport.ws_client import ConnectOptions, StubTokenProvider, TokenProvider

log = structlog.get_logger(__name__)


def build_app(settings: IntegrationSettings, *, ticket_deadline: timedelta) -> "IntegrationApp":
    log.warning("stub_in_use", component="betting, wallets", detail="the monolith behind BetDecisionPort is a stub")
    log.warning("stub_in_use", component="StubTokenProvider", detail="a static provider token from configuration")
    return IntegrationApp(
        settings,
        ticket_deadline=ticket_deadline,
        port=DjangoBetDecisionAdapter(),
        tokens=StubTokenProvider(settings.provider_token),
    )


class IntegrationApp:
    def __init__(
        self,
        settings: IntegrationSettings,
        *,
        ticket_deadline: timedelta,
        port: BetDecisionPort,
        tokens: TokenProvider,
    ) -> None:
        self._settings = settings
        self._tokens = tokens
        self.shutdown = Shutdown()
        self.health_port: int | None = None  # bound when serving; the setting may be 0
        self._heartbeats = Heartbeats(settings.liveness_stale_after)
        # Unique per process, so a restarted pod never mistakes its predecessor's leases for its own.
        self.owner = f"{settings.instance_name[:48]}-{secrets.token_hex(4)}"
        self._run_db = DbRunner(
            max_workers=settings.db_workers,
            statement_timeout=settings.db_statement_timeout,
            lock_timeout=settings.db_lock_timeout,
            idle_in_transaction_timeout=settings.db_idle_in_transaction_timeout,
            await_timeout=settings.db_await_timeout,
            admission_timeout=settings.db_admission_timeout,
        )
        self._queue: InboundQueue = asyncio.Queue(maxsize=settings.inbound_queue_size)
        self._health = Health(
            self._heartbeats,
            self.shutdown,
            last_db_success=lambda: self._run_db.last_success,
            ready_db_within=settings.readiness_db_within,
        )
        self._decisions = Decisions(self._run_db, port)
        self._relay = Relay(
            self._run_db,
            RelaySettings(
                owner=self.owner,
                lease=settings.lease,
                batch_size=settings.relay_batch_size,
                poll_interval=settings.relay_poll_interval,
                retransmit_after=ticket_deadline / 2,
                loss_window=settings.loss_window,
            ),
            self._heartbeats.loop("relay"),
        )
        self._sweeper = Sweeper(
            self._run_db,
            self._decisions,
            SweeperSettings(
                interval=settings.sweeper_interval,
                batch_size=settings.sweeper_batch_size,
                retry_failed_after=settings.sweeper_retry_failed_after,
            ),
            self._heartbeats.loop("sweeper"),
        )

    async def run(self) -> None:
        """Returns once a shutdown request has been drained (exit code 0); raises when a loop fails."""
        s = self._settings
        metrics.INBOUND_QUEUE_DEPTH.set_function(self._queue.qsize)
        try:
            async with serve_health(self._health, host=s.health_host, port=s.health_port) as port:
                self.health_port = port
                log.info("health_serving", port=port)
                if not await self._wait_until_started():
                    return
                try:
                    await supervise(self._loops())
                except* Drained:
                    log.info("integration_stopped")
        finally:
            # Off the loop and before it closes: units of work that outlived their caller finish first.
            await asyncio.to_thread(self._run_db.shutdown)

    async def _wait_until_started(self) -> bool:
        """Until the database is reachable and every migration is applied; the startup probe is red meanwhile.
        False when shutdown was requested first."""
        while not self.shutdown.requested:
            try:
                pending = await self._run_db(schema.pending_migrations)
            except TRANSIENT_DB_ERRORS as exc:
                log.warning("startup_waiting", reason="database", error=type(exc).__name__)
            else:
                if not pending:
                    self._health.mark_started()
                    log.info("integration_started", owner=self.owner, appliers=self._settings.appliers)
                    return True
                log.warning("startup_waiting", reason="migrations_pending", pending=pending)
            await self.shutdown.sleep(self._settings.startup_retry.total_seconds())
        return False

    def _loops(self) -> dict[str, Callable[[], Awaitable[object]]]:
        s = self._settings
        options = ConnectOptions(
            url=s.provider_url,
            open_timeout=s.ws_open_timeout,
            ping_interval=s.ws_ping_interval,
            ping_timeout=s.ws_ping_timeout,
            close_timeout=s.ws_close_timeout,
            send_timeout=s.ws_send_timeout,
            token_timeout=s.token_timeout,
        )
        backoff = Backoff(
            BackoffPolicy(
                retry_initial=s.reconnect_initial,
                retry_max=s.reconnect_max,
                standby_retry=s.standby_retry,
                displaced=s.displaced_backoff,
                auth_initial=s.auth_backoff_initial,
                auth_max=s.auth_backoff_max,
                stable_after=s.session_stable_after,
            )
        )
        connection = Connection(
            options=options,
            tokens=self._tokens,
            relay=self._relay,
            queue=self._queue,
            backoff=backoff,
            shutdown=self.shutdown,
            drain_timeout=s.drain_timeout,
            heartbeat=self._heartbeats.loop("connection"),
        )
        appliers = {f"applier-{n}": self._heartbeats.loop(f"applier-{n}") for n in range(s.appliers)}
        return {
            "connection": lambda: run_connection(connection),
            "sweeper": self._sweeper.run,
            **{
                name: partial(apply_messages, self._queue, self._decisions, heartbeat)
                for name, heartbeat in appliers.items()
            },
        }
