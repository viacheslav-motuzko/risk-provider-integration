from enum import StrEnum

from django.db import DataError, IntegrityError, InterfaceError, OperationalError

from apps.risk_integration.domain.errors import PermanentApplyError
from apps.risk_integration.persistence.db import OutcomeUnknown


class ApplyFailure(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"


def classify_apply_failure(exc: Exception) -> ApplyFailure | None:
    if isinstance(exc, OutcomeUnknown | OperationalError | InterfaceError):
        return ApplyFailure.TRANSIENT
    if isinstance(exc, IntegrityError | DataError | PermanentApplyError):
        return ApplyFailure.PERMANENT
    return None
