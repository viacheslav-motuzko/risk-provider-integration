from enum import StrEnum

from django.db import DataError, IntegrityError, InterfaceError, OperationalError
from psycopg import errors as pg

from apps.risk_integration.domain.errors import PermanentApplyError
from apps.risk_integration.persistence.db import NotAdmitted, OutcomeUnknown

TRANSIENT_DB_ERRORS: tuple[type[Exception], ...] = (OutcomeUnknown, NotAdmitted, OperationalError, InterfaceError)


class ApplyFailure(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"


def classify_apply_failure(exc: Exception) -> ApplyFailure | None:
    if isinstance(exc, TRANSIENT_DB_ERRORS):
        return ApplyFailure.TRANSIENT
    if isinstance(exc, IntegrityError | DataError | PermanentApplyError):
        return ApplyFailure.PERMANENT
    return None


def transient_kind(exc: Exception) -> str:
    """A bounded label for a transient error. A duplicate that waited on the ticket lock past `lock_timeout` is
    harmless and must not read as a failing database."""
    if isinstance(exc, OutcomeUnknown):
        return "outcome_unknown"
    if isinstance(exc, NotAdmitted):
        return "not_admitted"
    if isinstance(exc.__cause__, pg.LockNotAvailable):
        return "lock_timeout"
    if isinstance(exc.__cause__, pg.QueryCanceled):
        return "statement_timeout"
    return "db_error"
