"""In-process stand-in for the risk provider, for scenario tests. Test tooling, not production code.

It speaks the assumed wire format through `protocol.Envelope`, so a change to an assumption shows up here too.
The knobs are plain attributes; a test may change them at any time.
"""

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from apps.risk_integration.protocol.close_codes import CLOSE_ALREADY_CONNECTED
from apps.risk_integration.protocol.envelope import Envelope
from apps.risk_integration.protocol.messages import Decision, InboundType, OutboundType

WAIT = 5.0  # seconds; bounds a hang, never waited out when the scenario works


class TicketReply(StrEnum):
    ACCEPT = "ACCEPT"
    DECLINE = "DECLINE"
    SILENT = "SILENT"
    REFUSE = "REFUSE"  # an ERROR about the TICKET


class RejectNew(StrEnum):
    """How a second session is refused while one is open; `None` on the knob allows several sessions."""

    UPGRADE = "UPGRADE"  # HTTP 409 at the upgrade
    CLOSE = "CLOSE"  # accepted, then closed with the already-connected code


class FakeProvider:
    def __init__(self, *, token: str) -> None:
        self.token = token  # the one it accepts; a test may change it
        self.reject_new: RejectNew | None = None
        self.ticket_reply = TicketReply.ACCEPT
        self.duplicates = 1  # copies of each recommendation
        self.refuse_confirm = False
        self.refuse_cancellation = False
        self.received: list[Envelope] = []
        self.sessions_opened = 0
        self._arrived = asyncio.Condition()
        self._connections: set[ServerConnection] = set()
        self._server: Server | None = None

    async def __aenter__(self) -> Self:
        self._server = await serve(self._handle, "127.0.0.1", 0, process_request=self._authorize)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = next(iter(self._server.sockets)).getsockname()[:2]
        return f"ws://{host}:{port}"

    def frames(self, message_type: OutboundType, ticket_id: UUID | None = None) -> list[Envelope]:
        return [e for e in self.received if e.type == message_type and (ticket_id is None or e.ticket_id == ticket_id)]

    async def wait_for(self, message_type: OutboundType, ticket_id: UUID | None = None, *, count: int = 1) -> Envelope:
        """The latest such frame, once `count` of them have arrived."""
        async with asyncio.timeout(WAIT), self._arrived:
            await self._arrived.wait_for(lambda: len(self.frames(message_type, ticket_id)) >= count)
        return self.frames(message_type, ticket_id)[-1]

    async def wait_for_session(self, *, opened: int = 1) -> None:
        """Until a session is open and `opened` sessions have been accepted in all."""
        async with asyncio.timeout(WAIT), self._arrived:
            await self._arrived.wait_for(lambda: bool(self._connections) and self.sessions_opened >= opened)

    async def send(self, frame: str) -> None:
        """Send a frame to every open session: a late, unknown or malformed message."""
        for connection in list(self._connections):
            await connection.send(frame)

    async def close_sessions(self, code: int) -> None:
        for connection in list(self._connections):
            await connection.close(code)

    @staticmethod
    def recommendation(ticket_id: UUID, decision: Decision) -> str:
        return _frame(InboundType.RECOMMENDATION, ticket_id, {"decision": decision})

    @staticmethod
    def error(ticket_id: UUID | None, ref_type: OutboundType | None, code: str) -> str:
        payload: dict[str, Any] = {"code": code, "message": "refused by the fake provider"}
        if ref_type is not None:
            payload["ref_type"] = ref_type
        return _frame(InboundType.ERROR, ticket_id, payload)

    def _authorize(self, connection: ServerConnection, request: Request) -> Response | None:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid token\n")
        if self.reject_new is RejectNew.UPGRADE and self._connections:
            return connection.respond(HTTPStatus.CONFLICT, "already connected\n")
        return None

    async def _handle(self, connection: ServerConnection) -> None:
        if self.reject_new is RejectNew.CLOSE and self._connections:
            await connection.close(CLOSE_ALREADY_CONNECTED)
            return
        async with self._arrived:
            self._connections.add(connection)
            self.sessions_opened += 1
            self._arrived.notify_all()
        try:
            async for frame in connection:
                envelope = Envelope.model_validate_json(frame)
                async with self._arrived:
                    self.received.append(envelope)
                    self._arrived.notify_all()
                for reply in self._replies(envelope):
                    await connection.send(reply)
        except ConnectionClosed:
            pass  # the client went away; the next session is a new connection
        finally:
            self._connections.discard(connection)

    def _replies(self, envelope: Envelope) -> list[str]:
        ticket_id = envelope.ticket_id
        assert ticket_id is not None
        match envelope.type:
            case OutboundType.TICKET:
                match self.ticket_reply:
                    case TicketReply.ACCEPT | TicketReply.DECLINE:
                        return [self.recommendation(ticket_id, Decision(self.ticket_reply))] * self.duplicates
                    case TicketReply.REFUSE:
                        return [self.error(ticket_id, OutboundType.TICKET, "TICKET_REJECTED")]
            case OutboundType.CONFIRM_RECOMMENDATION if self.refuse_confirm:
                return [self.error(ticket_id, OutboundType.CONFIRM_RECOMMENDATION, "CONFIRM_TOO_LATE")]
            case OutboundType.REQUEST_CANCELLATION if self.refuse_cancellation:
                return [self.error(ticket_id, OutboundType.REQUEST_CANCELLATION, "ALREADY_SETTLED")]
        return []


def _frame(message_type: InboundType, ticket_id: UUID | None, payload: dict[str, Any]) -> str:
    return Envelope(type=message_type, ticket_id=ticket_id, ts=datetime.now(UTC), payload=payload).model_dump_json()
