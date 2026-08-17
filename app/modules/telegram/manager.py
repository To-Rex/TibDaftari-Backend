"""Bot manager — one python-telegram-bot Application per company, running inside the API event loop.

* `start()` schedules the bots in the background (the API never waits for Telegram); a bot with a bad
  token / no network is logged and skipped, the others keep running.
* `reload_company(id)` restarts one bot after `PUT /companies/{id}/telegram`.
* `deliver(session, msg)` is the outbox channel used by the messaging dispatcher (text or result PDF).

python-telegram-bot is imported lazily so a broken package cannot stop the API from booting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.infrastructure.db.models import Company, OutboxMessage
from app.infrastructure.db.session import session_scope
from app.modules.telegram import repository as repo
from app.modules.telegram import service

if TYPE_CHECKING:  # pragma: no cover
    from telegram.ext import Application

log = logging.getLogger("telegram")

START_TIMEOUT_SECONDS = 45
STOP_TIMEOUT_SECONDS = 10
NOT_CONFIGURED = "telegram_not_configured"


class BotManager:
    """Registry of running company bots (`company_id → Application`)."""

    def __init__(self) -> None:
        self._apps: dict[uuid.UUID, Application] = {}
        self._starting: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start every configured company bot in the background; returns immediately."""
        if self._starting and not self._starting.done():
            return
        self._starting = asyncio.create_task(self._start_all(), name="telegram-bots-start")

    async def _start_all(self) -> None:
        try:
            async with session_scope() as s:
                companies = await repo.companies_with_bot(s)
        except Exception:
            log.exception("telegram: could not load companies — bots not started")
            return
        if not companies:
            log.info("telegram: no company bots configured")
            return
        results = await asyncio.gather(*(self._start_company(c) for c in companies), return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        log.info("telegram: %d/%d company bots running", ok, len(companies))

    async def _start_company(self, company: Company) -> bool:
        """Start one bot; any failure is logged and swallowed (returns False)."""
        token = decrypt(company.telegram_bot_token_enc)
        if not token:
            log.warning("telegram[%s]: bot token cannot be decrypted — skipped", company.id)
            return False
        try:
            from app.modules.telegram.handlers import build_application

            application = build_application(token, company.id)
            async with self._lock:
                if company.id in self._apps:
                    return True
                self._apps[company.id] = application
            await asyncio.wait_for(self._run(application, company), START_TIMEOUT_SECONDS)
            log.info("telegram[%s]: bot @%s started", company.id, company.telegram_bot_username or "?")
            return True
        except Exception as exc:
            reason = str(exc).replace(token, "***")  # never log the bot token
            log.warning("telegram[%s]: bot not started (%s: %s)", company.id, exc.__class__.__name__, reason)
            app = self._apps.pop(company.id, None)
            if app is not None:
                await self._shutdown(app)
            return False

    @staticmethod
    async def _run(application: Application, company: Company) -> None:
        await application.initialize()
        await application.start()
        webhook_mode = bool((company.settings or {}).get("telegramWebhookSecret"))
        if not webhook_mode and application.updater is not None:
            await application.updater.start_polling(drop_pending_updates=True)

    @staticmethod
    async def _shutdown(application: Application) -> None:
        for step in ("updater", "stop", "shutdown"):
            try:
                if step == "updater":
                    if application.updater is not None and application.updater.running:
                        await asyncio.wait_for(application.updater.stop(), STOP_TIMEOUT_SECONDS)
                elif step == "stop":
                    if application.running:
                        await asyncio.wait_for(application.stop(), STOP_TIMEOUT_SECONDS)
                else:
                    await asyncio.wait_for(application.shutdown(), STOP_TIMEOUT_SECONDS)
            except Exception as exc:
                log.debug("telegram: %s during shutdown: %s", exc.__class__.__name__, exc)

    async def stop(self) -> None:
        """Stop all bots (used by the app lifespan)."""
        if self._starting and not self._starting.done():
            self._starting.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._starting
        apps = list(self._apps.values())
        self._apps.clear()
        for app in apps:
            await self._shutdown(app)

    async def stop_company(self, company_id: uuid.UUID) -> None:
        """Stop one company bot if it is running here."""
        app = self._apps.pop(company_id, None)
        if app is not None:
            await self._shutdown(app)

    async def reload_company(self, company_id: uuid.UUID | str) -> bool:
        """Stop and (if still configured) start the company bot. Returns True when it is running afterwards."""
        cid = uuid.UUID(str(company_id))
        try:
            await self.stop_company(cid)
            async with session_scope() as s:
                company = await repo.get_company(s, cid)
            if not company or not company.telegram_bot_token_enc or not company.is_active:
                return False
            return await self._start_company(company)
        except Exception:
            log.exception("telegram[%s]: reload failed", cid)
            return False

    # ------------------------------------------------------------------ introspection

    def is_running(self, company_id: uuid.UUID | str) -> bool:
        """True when this process runs the company's Application."""
        app = self._apps.get(uuid.UUID(str(company_id)))
        return bool(app is not None and app.running)

    def application_for(self, company_id: uuid.UUID | str) -> Application | None:
        """Running Application of the company (webhook router), or None."""
        return self._apps.get(uuid.UUID(str(company_id)))

    # ------------------------------------------------------------------ outbox delivery

    @contextlib.asynccontextmanager
    async def _open_bot(self, company: Company) -> AsyncIterator[Any]:
        """The running bot of the company, or a lightweight on-demand `telegram.Bot` (delivery only)."""
        app = self._apps.get(company.id)
        if app is not None and app.running:
            yield app.bot
            return
        token = decrypt(company.telegram_bot_token_enc)
        if not token:
            yield None
            return
        from telegram import Bot

        async with Bot(token) as bot:
            yield bot

    async def deliver(self, session: AsyncSession, msg: OutboxMessage) -> bool:
        """Deliver an outbox row (`channel=telegram`, `to`=chat id).

        `result_ready` with `payload.documentId` → the document PDF with the text as caption; other kinds → text.
        Returns True on success; False (with `msg.error` set) when the company has no bot / the chat rejected the
        message; raises on transient Telegram errors so the dispatcher retries with backoff."""
        from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut

        company = await repo.get_company(session, msg.company_id)
        if not company or not company.telegram_bot_token_enc:
            msg.error = NOT_CONFIGURED
            return False
        try:
            async with self._open_bot(company) as bot:
                if bot is None:
                    msg.error = NOT_CONFIGURED
                    return False
                sent = await self._send(session, bot, msg)
        except (RetryAfter, TimedOut, NetworkError):
            raise
        except InvalidToken:
            msg.error = "telegram_invalid_token"
            return False
        except Forbidden as exc:  # patient blocked the bot / chat deleted — permanent
            msg.error = f"telegram_forbidden: {exc}"[:500]
            return False
        except BadRequest as exc:  # unknown chat id, bad payload — retrying cannot help
            msg.error = f"telegram_bad_request: {exc}"[:500]
            return False
        if sent is not None:
            msg.provider_message_id = str(getattr(sent, "message_id", "") or "")[:64] or None
        return True

    async def _send(self, session: AsyncSession, bot: Any, msg: OutboxMessage) -> Any:
        chat_id = msg.to
        document_id = (msg.payload or {}).get("documentId") or (str(msg.document_id) if msg.document_id else None)
        if msg.kind == "result_ready" and document_id:
            document = await repo.get_document(session, msg.company_id, uuid.UUID(str(document_id)))
            if document is not None:
                pdf = await service.document_pdf(session, document)
                return await bot.send_document(chat_id=chat_id, document=pdf, filename=service.pdf_filename(document), caption=(msg.text or "")[:1024])
        return await bot.send_message(chat_id=chat_id, text=msg.text)


bot_manager = BotManager()
