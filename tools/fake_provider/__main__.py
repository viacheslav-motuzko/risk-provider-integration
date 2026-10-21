"""Runs the fake provider on its own, for the docker compose demo: `python -m tools.fake_provider`.

FAKE_PROVIDER_TOKEN is the token it accepts; FAKE_PROVIDER_REPLY is ACCEPT, DECLINE, SILENT or REFUSE. Test
tooling, not production code.
"""

import asyncio
import logging
import os

from tools.fake_provider.server import FakeProvider, TicketReply


async def main() -> None:
    fake = FakeProvider(
        token=os.environ.get("FAKE_PROVIDER_TOKEN", "dev-token"),
        host=os.environ.get("FAKE_PROVIDER_HOST", "0.0.0.0"),  # noqa: S104 - reached from the app container
        port=int(os.environ.get("FAKE_PROVIDER_PORT", "8765")),
    )
    fake.ticket_reply = TicketReply(os.environ.get("FAKE_PROVIDER_REPLY", TicketReply.ACCEPT))
    async with fake:
        logging.info("fake provider listening on %s, replying %s", fake.url, fake.ticket_reply)
        await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
