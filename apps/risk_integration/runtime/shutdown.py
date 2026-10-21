"""A shutdown request (SIGTERM) and the exceptions that carry it out of the loops.

The connection task owns the drain, because it owns the session: the session group stops, the appliers finish
the queue, the final messages owed go out once, the session closes with 1001, and `Drained` ends the process
cleanly. Recommendations still owed for `SENT` tickets are not waited for: the next session's re-send, in this
pod's successor or another instance, brings them back.
"""

import asyncio
from contextlib import suppress
from typing import NoReturn

import structlog

log = structlog.get_logger(__name__)


class StopRequested(Exception):
    """Ends a session group so that the connection task can drain the session."""


class Drained(Exception):
    """The connection task has drained its session, or had none. Ends the process with exit code 0."""


class Shutdown:
    def __init__(self) -> None:
        self._requested = asyncio.Event()

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def request(self, reason: str) -> None:
        if not self._requested.is_set():
            log.info("shutdown_requested", reason=reason)
            self._requested.set()

    async def stop_session(self) -> NoReturn:
        await self._requested.wait()
        raise StopRequested

    async def sleep(self, seconds: float) -> None:
        """Sleeps, but returns as soon as shutdown is requested."""
        with suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._requested.wait()
