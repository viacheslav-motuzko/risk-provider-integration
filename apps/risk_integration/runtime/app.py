"""Composition root: builds every part from the settings and runs the loops under one supervisor."""

import asyncio
import secrets
from datetime import timedelta

import structlog

from apps.risk_integration.adapters.monolith import DjangoBetDecisionAdapter
from apps.risk_integration.persistence.db import DbRunner
from apps.risk_integration.ports import BetDecisionPort
from apps.risk_integration.runtime.backoff import Backoff, BackoffPolicy
from apps.risk_integration.runtime.config import IntegrationSettings
from apps.risk_integration.runtime.connection import run_connection
from apps.risk_integration.runtime.decisions import Decisions
from apps.risk_integration.runtime.inbound import InboundQueue, apply_messages
from apps.risk_integration.runtime.relay import Relay, RelaySettings
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
        )
        self._sweeper = Sweeper(
            self._run_db,
            self._decisions,
            SweeperSettings(
                interval=settings.sweeper_interval,
                batch_size=settings.sweeper_batch_size,
                retry_failed_after=settings.sweeper_retry_failed_after,
            ),
        )

    async def run(self) -> None:
        """Runs until a loop fails, then raises; the process exits non-zero."""
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
        loops = {
            "connection": lambda: run_connection(options, self._tokens, self._relay, self._queue, backoff),
            "sweeper": self._sweeper.run,
            **{f"applier-{n}": lambda: apply_messages(self._queue, self._decisions) for n in range(s.appliers)},
        }
        log.info("integration_starting", owner=self.owner, appliers=s.appliers)
        try:
            await supervise(loops)
        finally:
            # Off the loop and before it closes: units of work that outlived their caller finish first.
            await asyncio.to_thread(self._run_db.shutdown)
