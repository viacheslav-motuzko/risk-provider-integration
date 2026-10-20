from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict


class Envelope(BaseModel):
    """ASSUMPTION: every frame in both directions is `{type, ticket_id, ts, payload}` as a JSON text frame.

    `ticket_id` is optional: an `ERROR` about a frame without a ticket has none (ASSUMPTION).
    `ts` is the sender's clock, used for logs and latency metrics only, never in a decision: clocks may differ.
    Unknown top-level fields are ignored so the provider can extend the envelope without breaking us.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    type: str
    ticket_id: UUID | None = None
    ts: AwareDatetime | None = None
    payload: dict[str, Any] = {}
