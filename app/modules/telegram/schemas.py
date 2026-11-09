"""Telegram module DTOs (camelCase on the wire)."""

from __future__ import annotations

from app.core.schemas import CamelModel


class TelegramStatusOut(CamelModel):
    """`GET /units/{unitId}/telegram/status` — the bot account the unit resolves to (its own, or an ancestor's
    when `inherited`), the bot's username and whether its Application is running in this process."""

    connected: bool
    running: bool
    bot_username: str | None = None
    account_id: str | None = None
    #: the account sits on an ancestor of the unit, not on the unit itself
    inherited: bool = False
    effective_unit_id: str | None = None


class WebhookAck(CamelModel):
    """`POST /telegram/webhook/{accountId}/{secret}` — Telegram only needs a 200; `ok` mirrors its convention."""

    ok: bool
