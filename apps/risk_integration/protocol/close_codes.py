"""Classification of how a provider session ended or was refused, which decides the reconnect backoff.

Takes plain integers so the transport maps its library's exceptions here and this module stays library-free.
"""

from enum import StrEnum


class SessionOutcome(StrEnum):
    AUTH_FAILED = "auth_failed"
    DISPLACED = "displaced"
    ALREADY_CONNECTED = "already_connected"
    PLANNED = "planned"
    TRANSIENT = "transient"


# ASSUMPTION: provider-specific close codes in the 4000-4999 range reserved for applications (RFC 6455).
CLOSE_PLANNED = 4000  # the provider ends a session at its maximum lifetime
CLOSE_AUTH_FAILED = 4001
CLOSE_DISPLACED = 4002
CLOSE_ALREADY_CONNECTED = 4003  # the provider refuses a second session
# Ours, when a draining process closes its session (RFC 6455 "going away").
CLOSE_GOING_AWAY = 1001

_BY_CLOSE_CODE = {
    CLOSE_PLANNED: SessionOutcome.PLANNED,
    CLOSE_AUTH_FAILED: SessionOutcome.AUTH_FAILED,
    CLOSE_DISPLACED: SessionOutcome.DISPLACED,
    CLOSE_ALREADY_CONNECTED: SessionOutcome.ALREADY_CONNECTED,
}

# ASSUMPTION: a refused upgrade carries the reason in the HTTP status.
_BY_HANDSHAKE_STATUS = {
    401: SessionOutcome.AUTH_FAILED,
    403: SessionOutcome.AUTH_FAILED,
    409: SessionOutcome.ALREADY_CONNECTED,
}


def classify_close(code: int | None) -> SessionOutcome:
    """Classify a close received from the provider; `None` means the connection dropped without a close frame.

    Standard codes (1000 normal, 1001 going away, 1006 abnormal, 1011 server error) and unknown codes are
    transient: reconnecting with backoff is always safe, while a wrong fatal class would stop the integration.
    """
    if code is None:
        return SessionOutcome.TRANSIENT
    return _BY_CLOSE_CODE.get(code, SessionOutcome.TRANSIENT)


def classify_handshake_rejection(status: int) -> SessionOutcome:
    """Classify an HTTP status that refused the WebSocket upgrade."""
    return _BY_HANDSHAKE_STATUS.get(status, SessionOutcome.TRANSIENT)
