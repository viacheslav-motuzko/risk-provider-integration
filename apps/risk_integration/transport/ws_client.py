"""One provider session over `websockets`. The library's exceptions become `SessionEnded` with a classified
outcome here, so nothing above this module depends on the library."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import NoReturn, Protocol

from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from apps.risk_integration.protocol.close_codes import SessionOutcome, classify_close, classify_handshake_rejection

# The library logs request headers, the bearer token among them, at DEBUG. Its own logger pinned at INFO keeps
# the token out of the logs whatever the global level.
_LIBRARY_LOGGER = logging.getLogger("apps.risk_integration.transport.websockets")
_LIBRARY_LOGGER.setLevel(logging.INFO)


@dataclass(frozen=True)
class ConnectOptions:
    url: str
    open_timeout: timedelta
    ping_interval: timedelta
    ping_timeout: timedelta
    close_timeout: timedelta
    send_timeout: timedelta


class SessionEnded(Exception):
    """The session was refused, dropped or closed. `outcome` decides what happens next."""

    def __init__(self, outcome: SessionOutcome, detail: str) -> None:
        super().__init__(f"{outcome}: {detail}")
        self.outcome = outcome


class TokenProvider(Protocol):
    async def token(self) -> SecretStr:
        """A token for the next connect; called before every connect."""
        ...


class StubTokenProvider:
    """STUB: returns the configured static token."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    async def token(self) -> SecretStr:
        return self._token


class ProviderSession:
    def __init__(self, ws: ClientConnection, *, send_timeout: timedelta) -> None:
        self._ws = ws
        self._send_timeout = send_timeout.total_seconds()
        self.connection_id = str(ws.id)

    async def send(self, frame: str) -> None:
        try:
            async with asyncio.timeout(self._send_timeout):
                await self._ws.send(frame)
        except ConnectionClosed as exc:
            raise _ended(exc) from exc
        except TimeoutError as exc:
            # The peer stopped reading and the write buffer is full: the session is no use any more.
            raise SessionEnded(SessionOutcome.TRANSIENT, "send timed out") from exc

    async def recv(self) -> str | bytes:
        try:
            return await self._ws.recv()
        except ConnectionClosed as exc:
            raise _ended(exc) from exc

    async def closed(self) -> NoReturn:
        """Raises when the connection closes, also while nobody reads it (the reader waits on a full queue)."""
        await self._ws.wait_closed()
        exc = self._ws.protocol.close_exc
        raise _ended(exc) from exc


@asynccontextmanager
async def open_session(options: ConnectOptions, token: SecretStr) -> AsyncIterator[ProviderSession]:
    try:
        ws = await connect(
            options.url,
            # ASSUMPTION: the token goes as a bearer token in the Authorization header of the upgrade request.
            additional_headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            open_timeout=options.open_timeout.total_seconds(),
            ping_interval=options.ping_interval.total_seconds(),
            ping_timeout=options.ping_timeout.total_seconds(),
            close_timeout=options.close_timeout.total_seconds(),
            logger=_LIBRARY_LOGGER,
        )
    except InvalidStatus as exc:
        status = exc.response.status_code
        raise SessionEnded(classify_handshake_rejection(status), f"upgrade refused with HTTP {status}") from exc
    except (OSError, TimeoutError, InvalidHandshake) as exc:
        raise SessionEnded(SessionOutcome.TRANSIENT, f"connect failed: {type(exc).__name__}") from exc
    try:
        yield ProviderSession(ws, send_timeout=options.send_timeout)
    finally:
        await ws.close()  # bounded by close_timeout


def _ended(exc: ConnectionClosed) -> SessionEnded:
    code = exc.rcvd.code if exc.rcvd is not None else None
    return SessionEnded(classify_close(code), f"closed with code {code}" if code is not None else "connection lost")
