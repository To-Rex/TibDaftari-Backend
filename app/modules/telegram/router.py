"""Telegram HTTP surface: optional webhook receiver + bot status for the settings page."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Body

from app.api.deps import DbSession, Staff
from app.core.exceptions import NotFoundError
from app.modules.telegram import repository as repo
from app.modules.telegram import service
from app.modules.telegram.manager import bot_manager
from app.modules.telegram.schemas import TelegramStatusOut, WebhookAck

log = logging.getLogger("telegram")
router = APIRouter()


@router.post("/telegram/webhook/{company_id}/{secret}", response_model=WebhookAck, summary="Telegram webhook (optional; polling is the default)")
async def webhook(company_id: uuid.UUID, secret: str, session: DbSession, payload: Any = Body(default=None)) -> WebhookAck:
    """Accepts a Telegram Update for the company bot. 404 unless `settings.telegramWebhookSecret` is set and matches;
    the update is queued into the running Application (dropped with a log line when the bot is not running here)."""
    company = await repo.get_company(session, company_id)
    if not service.webhook_secret_ok(company, secret):
        raise NotFoundError("Not found")
    application = bot_manager.application_for(company_id)
    if application is None or not application.running or not isinstance(payload, dict):
        log.info("telegram[%s]: webhook update dropped (bot not running here)", company_id)
        return WebhookAck(ok=True)
    try:
        from telegram import Update

        update = Update.de_json(payload, application.bot)
        await application.update_queue.put(update)
    except Exception:
        log.exception("telegram[%s]: webhook update rejected", company_id)
    return WebhookAck(ok=True)


@router.get("/companies/{company_id}/telegram/status", response_model=TelegramStatusOut, summary="Company bot status")
async def status(company_id: uuid.UUID, staff: Staff, session: DbSession) -> TelegramStatusOut:
    staff.require("admin.settings.write").scope(company_id)
    company = await repo.get_company(session, company_id)
    if not company:
        raise NotFoundError("Kompaniya topilmadi")
    return service.status_out(company, bot_manager.is_running(company_id))
