"""Telegram HTTP surface: optional webhook receiver (per bot account) + bot status for a unit's settings page."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Body

from app.api.deps import DbSession, Scope, Staff
from app.core.exceptions import NotFoundError
from app.modules.telegram import repository as repo
from app.modules.telegram import service
from app.modules.telegram.manager import bot_manager
from app.modules.telegram.schemas import TelegramStatusOut, WebhookAck
from app.modules.units import repository as units_repo
from app.modules.units import service as units_svc

log = logging.getLogger("telegram")
router = APIRouter()


@router.post("/telegram/webhook/{account_id}/{secret}", response_model=WebhookAck, summary="Telegram webhook (optional; polling is the default)")
async def webhook(account_id: uuid.UUID, secret: str, session: DbSession, payload: Any = Body(default=None)) -> WebhookAck:
    """Accepts a Telegram Update for the bot of `account_id`. 404 unless the account's unit has
    `telegramWebhookSecret` in its effective settings and it matches; the update is queued into the running
    Application (dropped with a log line when the bot is not running here)."""
    account = await repo.get_account(session, account_id)
    if account is None or not account.is_active:
        raise NotFoundError("Not found")
    effective = await units_svc.effective_settings(session, account.unit_id)
    if not service.webhook_secret_ok(effective, secret):
        raise NotFoundError("Not found")
    application = bot_manager.application_for(account_id)
    if application is None or not application.running or not isinstance(payload, dict):
        log.info("telegram[%s]: webhook update dropped (bot not running here)", account_id)
        return WebhookAck(ok=True)
    try:
        from telegram import Update

        update = Update.de_json(payload, application.bot)
        await application.update_queue.put(update)
    except Exception:
        log.exception("telegram[%s]: webhook update rejected", account_id)
    return WebhookAck(ok=True)


@router.get("/units/{unit_id}/telegram/status", response_model=TelegramStatusOut, summary="Unit bot status")
async def status(scope: Scope, staff: Staff, session: DbSession) -> TelegramStatusOut:
    """The bot the unit uses — its own account or the nearest ancestor's (`inherited`) — and whether it runs here."""
    staff.require("admin.settings.write")
    account = await units_repo.resolve_account(session, scope.id, "telegram")
    return service.status_out(scope.id, account, bot_manager.is_running(account.id) if account is not None else False)
