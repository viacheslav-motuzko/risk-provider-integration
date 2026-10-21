import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import NoReturn


class LoopReturned(RuntimeError):
    """A loop that must run until cancelled returned. A bug: it ends the group like a failure would."""


async def supervise(loops: Mapping[str, Callable[[], Awaitable[object]]]) -> NoReturn:
    """Run the loops in one `TaskGroup`. The first that fails or returns cancels the others, and the group raises.
    A loop that returned would cancel nothing on its own, so returning is turned into a failure."""
    async with asyncio.TaskGroup() as group:
        for name, loop in loops.items():
            group.create_task(_until_cancelled(name, loop), name=name)
    # Every loop ends in an exception, so the group always raises; only an empty mapping gets here.
    raise LoopReturned("no loops")


async def _until_cancelled(name: str, loop: Callable[[], Awaitable[object]]) -> None:
    await loop()
    raise LoopReturned(name)
