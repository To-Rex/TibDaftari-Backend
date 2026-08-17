"""Telegram module DTOs (camelCase on the wire)."""

from __future__ import annotations

from app.core.schemas import CamelModel


class TelegramStatusOut(CamelModel):
    """`GET /companies/{id}/telegram/status` — bot configured (token stored), its username, and whether
    the long-polling application is running in this process."""

    connected: bool
    bot_username: str | None = None
    running: bool


class WebhookAck(CamelModel):
    """`POST /telegram/webhook/{companyId}/{secret}` — Telegram only needs a 200; `ok` mirrors its convention."""

    ok: bool
