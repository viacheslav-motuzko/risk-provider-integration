import asyncio
import signal
from typing import Any

import structlog
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from pydantic import ValidationError

from apps.risk_integration.runtime.app import IntegrationApp, build_app
from apps.risk_integration.runtime.config import IntegrationSettings
from apps.risk_integration.runtime.logs import configure_logging

log = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = (
        "Run the risk provider integration: the provider session, relay, appliers and sweeper, with probes and "
        "metrics on their own port. SIGTERM drains and exits 0; a failed loop exits non-zero."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            integration = IntegrationSettings()
        except ValidationError as exc:
            raise CommandError(f"invalid RISK_* settings: {_describe(exc)}") from None
        configure_logging(integration.log_level)
        # The default is Django's (0) because the settings module is shared with the web pods; this process sets it.
        if settings.DATABASES["default"]["CONN_MAX_AGE"] == 0:
            log.warning("db_connections_not_persistent", detail="DATABASE_CONN_MAX_AGE is 0: a new connection per unit")
        asyncio.run(_run(build_app(integration, ticket_deadline=settings.RISK_TICKET_DEADLINE)))


async def _run(app: IntegrationApp) -> None:
    # Before the app's slow start (waiting for the database): as PID 1 in a container, Python ignores SIGTERM
    # until it has a handler.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, app.shutdown.request, sig.name)
    await app.run()


def _describe(exc: ValidationError) -> str:
    # Names and messages only: an input value may be a secret.
    errors = exc.errors(include_input=False, include_url=False, include_context=False)
    return "; ".join(f"RISK_{'_'.join(str(p) for p in e['loc']).upper() or '*'}: {e['msg']}" for e in errors)
