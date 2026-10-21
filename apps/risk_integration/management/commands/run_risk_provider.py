import asyncio
from typing import Any

import structlog
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from pydantic import ValidationError

from apps.risk_integration.runtime.app import build_app
from apps.risk_integration.runtime.config import IntegrationSettings

log = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = "Run the risk provider integration: the provider session, relay, appliers and sweeper. Exits on failure."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            integration = IntegrationSettings()
        except ValidationError as exc:
            raise CommandError(f"invalid RISK_* settings: {_describe(exc)}") from None
        # The default is 0 because the settings module is shared with the web pods; this process sets it.
        if settings.DATABASES["default"]["CONN_MAX_AGE"] == 0:
            log.warning("db_connections_not_persistent", detail="DATABASE_CONN_MAX_AGE is 0: a new connection per unit")
        asyncio.run(build_app(integration, ticket_deadline=settings.RISK_TICKET_DEADLINE).run())


def _describe(exc: ValidationError) -> str:
    # Names and messages only: an input value may be a secret.
    errors = exc.errors(include_input=False, include_url=False, include_context=False)
    return "; ".join(f"RISK_{'_'.join(str(p) for p in e['loc']).upper() or '*'}: {e['msg']}" for e in errors)
