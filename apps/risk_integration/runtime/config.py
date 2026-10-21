"""Integration settings, read once from `RISK_*` environment variables when the command starts. Durations are
given in seconds. The monolith's web pods never load this, so they need no provider variables."""

import socket
from datetime import timedelta
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _seconds(value: object) -> object:
    # pydantic reads a numeric string as an ISO 8601 duration and fails; environment values are seconds.
    return timedelta(seconds=float(value)) if isinstance(value, str) else value


type Duration = Annotated[timedelta, BeforeValidator(_seconds), Field(gt=timedelta(0))]


class IntegrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RISK_", frozen=True)

    provider_url: str
    # STUB: a static token.
    provider_token: SecretStr
    provider_allow_plaintext: bool = False  # ws:// for local development and tests only
    instance_name: str = Field(default_factory=socket.gethostname, min_length=1)

    appliers: int = Field(4, ge=1, le=64)
    inbound_queue_size: int = Field(64, ge=1)

    relay_poll_interval: Duration = timedelta(milliseconds=100)
    relay_batch_size: int = Field(50, ge=1)
    # Tickets claimed by a process that died wait this long, against a 10 s deadline: kept short.
    lease: Duration = timedelta(seconds=6)
    sweeper_interval: Duration = timedelta(seconds=1)
    sweeper_batch_size: int = Field(100, ge=1)
    sweeper_retry_failed_after: Duration = timedelta(seconds=5)

    ws_open_timeout: Duration = timedelta(seconds=10)
    ws_ping_interval: Duration = timedelta(seconds=20)
    ws_ping_timeout: Duration = timedelta(seconds=20)
    ws_close_timeout: Duration = timedelta(seconds=10)
    ws_send_timeout: Duration = timedelta(seconds=2)

    db_statement_timeout: Duration = timedelta(seconds=2)
    db_lock_timeout: Duration = timedelta(seconds=1)
    db_idle_in_transaction_timeout: Duration = timedelta(seconds=5)
    db_await_timeout: Duration = timedelta(seconds=3)
    db_admission_timeout: Duration = timedelta(seconds=5)

    @property
    def loss_window(self) -> timedelta:
        """How long a dead connection can look alive: frames written in this time before it is noticed may be
        lost, so final messages sent in it are re-sent on the next session."""
        return self.ws_ping_interval + self.ws_ping_timeout

    @property
    def db_workers(self) -> int:
        # One per applier, plus the relay and the sweeper, so a burst of applies cannot starve sending or deadlines.
        return self.appliers + 2

    @field_validator("provider_url")
    @classmethod
    def _websocket_url(cls, value: str) -> str:
        if urlsplit(value).scheme not in ("ws", "wss"):
            raise ValueError("must be a ws:// or wss:// URL")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if urlsplit(self.provider_url).scheme == "ws" and not self.provider_allow_plaintext:
            raise ValueError("RISK_PROVIDER_URL must use wss:// unless RISK_PROVIDER_ALLOW_PLAINTEXT is set")
        if self.db_await_timeout <= self.db_statement_timeout:
            raise ValueError("RISK_DB_AWAIT_TIMEOUT must exceed RISK_DB_STATEMENT_TIMEOUT")
        # A relay that holds a row longer than its lease lets another claim it: a duplicate send, which the
        # provider absorbs, but not one to cause on every slow send.
        if self.lease <= self.ws_send_timeout + self.db_await_timeout:
            raise ValueError("RISK_LEASE must exceed RISK_WS_SEND_TIMEOUT + RISK_DB_AWAIT_TIMEOUT")
        return self
