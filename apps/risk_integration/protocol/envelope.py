from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict


class Envelope(BaseModel):
    """Every frame in both directions: `{type, ticket_id, ts, payload}` as a JSON text frame (ASSUMPTION A-11).

    `ticket_id` is optional because an `ERROR` about a frame without a ticket has none (ASSUMPTION A-02).
    `ts` is the sender's clock: logged and used for latency metrics only, never in a decision (D12).
    Unknown top-level fields are ignored so the provider can extend the envelope without breaking us.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    type: str
    ticket_id: UUID | None = None
    ts: AwareDatetime | None = None
    payload: dict[str, Any] = {}
