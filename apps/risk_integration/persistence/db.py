"""The async/sync seam: every unit of ORM work runs through `DbRunner` (D5)."""

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta

from django.db import close_old_connections, connection, transaction


class OutcomeUnknown(Exception):
    """The caller stopped waiting, but the unit of work may still commit (D5). Re-read state before acting."""


class DbRunner:
    """Runs sync DB functions from the event loop: `await run_db(fn, *args)`.

    Each call is one transaction with `SET LOCAL` timeouts on a worker thread. Admission is bounded by a
    semaphore whose permit is released when the worker thread finishes, not when the caller stops waiting, so
    at most `max_workers` units of work are ever running or queued.
    """

    def __init__(
        self,
        *,
        max_workers: int,
        statement_timeout: timedelta,
        lock_timeout: timedelta,
        idle_in_transaction_timeout: timedelta,
        await_timeout: timedelta,
    ) -> None:
        if await_timeout <= statement_timeout:
            raise ValueError("await_timeout must exceed statement_timeout")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="run_db")
        self._permits = asyncio.Semaphore(max_workers)
        self._timeouts = (_ms(statement_timeout), _ms(lock_timeout), _ms(idle_in_transaction_timeout))
        self._await_timeout = await_timeout.total_seconds()

    async def __call__[**P, T](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        loop = asyncio.get_running_loop()
        await self._permits.acquire()
        try:
            # run_in_executor would not copy context variables (log context) into the worker.
            ctx = contextvars.copy_context()
            work: Future[T] = self._executor.submit(ctx.run, self._unit_of_work, functools.partial(fn, *args, **kwargs))
        except BaseException:
            self._permits.release()
            raise
        work.add_done_callback(lambda _: loop.call_soon_threadsafe(self._permits.release))
        try:
            async with asyncio.timeout(self._await_timeout):
                return await asyncio.wrap_future(work)
        except TimeoutError as exc:
            raise OutcomeUnknown(getattr(fn, "__name__", repr(fn))) from exc

    def shutdown(self) -> None:
        """Wait for running units of work; drop queued ones. Call after the last `run_db` has returned."""
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _unit_of_work[T](self, call: Callable[[], T]) -> T:
        close_old_connections()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true), "
                        "set_config('idle_in_transaction_session_timeout', %s, true)",
                        self._timeouts,
                    )
                return call()
        finally:
            close_old_connections()


def _ms(value: timedelta) -> str:
    return str(int(value.total_seconds() * 1000))
