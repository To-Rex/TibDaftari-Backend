"""Bot manager — one python-telegram-bot Application per Telegram account (`messaging_accounts`, channel
'telegram'), running inside the API event loop.

* `start()` schedules the bots in the background (the API never waits for Telegram); a bot with a bad
  token / no network is logged and skipped, the others keep running.
* `reload_account(id)` restarts one bot after `PUT /units/{id}/telegram`.
* `deliver(session, msg)` is the outbox channel used by the messaging dispatcher (text or result PDF).

The token is the account's own (decrypted) key, or `TELEGRAM_BOT_TOKEN` — the platform bot — when the account
stores none. python-telegram-bot is imported lazily so a broken package cannot stop the API from booting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models import MessagingAccount, OutboxMessage
from app.infrastructure.db.session import session_scope
from app.modules.telegram import repository as repo
from app.modules.telegram import service
from app.modules.telegram.service import BotSpec
from app.modules.units import repository as units_repo

if TYPE_CHECKING:  # pragma: no cover
    from telegram.ext import Application

log = logging.getLogger("telegram")

START_TIMEOUT_SECONDS = 45
STOP_TIMEOUT_SECONDS = 10
NOT_CONFIGURED = "telegram_not_configured"
NO_TOKEN = "no bot token (the account stores none and TELEGRAM_BOT_TOKEN is empty)"


class BotManager:
    """Registry of running bots (`account_id → Application`)."""

    def __init__(self) -> None:
        self._apps: dict[uuid.UUID, Application] = {}
        self._starting: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start every configured bot in the background; returns immediately."""
        if self._starting and not self._starting.done():
            return
        self._starting = asyncio.create_task(self._start_all(), name="telegram-bots-start")

    async def _start_all(self) -> None:
        try:
            async with session_scope() as s:
                accounts = await repo.accounts_with_bot(s)
                specs = [await service.bot_spec(s, a) for a in accounts]
        except Exception:
            log.exception("telegram: could not load bot accounts — bots not started")
            return
        runnable: list[BotSpec] = []
        for spec in specs:
            if spec.token:
                runnable.append(spec)
            else:
                log.warning("telegram[%s]: %s — skipped", spec.account_id, NO_TOKEN)
        if not runnable:
            log.info("telegram: no bots configured")
            return
        results = await asyncio.gather(*(self._start_bot(spec) for spec in runnable), return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        log.info("telegram: %d/%d bots running", ok, len(runnable))

    async def _start_bot(self, spec: BotSpec) -> bool:
        """Start one bot; any failure is logged and swallowed (returns False)."""
        token = spec.token
        if not token:
            log.warning("telegram[%s]: %s — skipped", spec.account_id, NO_TOKEN)
            return False
        try:
            from app.modules.telegram.handlers import build_application

            application = build_application(spec)
            async with self._lock:
                if spec.account_id in self._apps:
                    return True
                self._apps[spec.account_id] = application
            await asyncio.wait_for(self._run(application, spec), START_TIMEOUT_SECONDS)
            log.info("telegram[%s]: bot @%s started for unit %s", spec.account_id, spec.bot_username or "?", spec.unit_id)
            return True
        except Exception as exc:
            reason = str(exc).replace(token, "***")  # never log the bot token
            log.warning("telegram[%s]: bot not started (%s: %s)", spec.account_id, exc.__class__.__name__, reason)
            app = self._apps.pop(spec.account_id, None)
            if app is not None:
                await self._shutdown(app)
            return False

    @staticmethod
    async def _run(application: Application, spec: BotSpec) -> None:
        await application.initialize()
        await application.start()
        if not spec.webhook and application.updater is not None:
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

    async def stop_account(self, account_id: uuid.UUID) -> None:
        """Stop one bot if it is running here."""
        app = self._apps.pop(account_id, None)
        if app is not None:
            await self._shutdown(app)

    async def reload_account(self, account_id: uuid.UUID | str) -> bool:
        """Stop and (if still configured) start the bot of the account. Returns True when it is running afterwards."""
        aid = uuid.UUID(str(account_id))
        try:
            await self.stop_account(aid)
            async with session_scope() as s:
                account = await repo.get_account(s, aid)
                if account is None or not account.is_active:
                    return False
                spec = await service.bot_spec(s, account)
            return await self._start_bot(spec)
        except Exception:
            log.exception("telegram[%s]: reload failed", aid)
            return False

    # ------------------------------------------------------------------ introspection

    def is_running(self, account_id: uuid.UUID | str) -> bool:
        """True when this process runs the account's Application."""
        app = self._apps.get(uuid.UUID(str(account_id)))
        return bool(app is not None and app.running)

    def application_for(self, account_id: uuid.UUID | str) -> Application | None:
        """Application of the account (webhook router), or None when it is not running here."""
        return self._apps.get(uuid.UUID(str(account_id)))

    # ------------------------------------------------------------------ outbox delivery

    @contextlib.asynccontextmanager
    async def _open_bot(self, account_id: uuid.UUID, token: str) -> AsyncIterator[Any]:
        """The running bot of the account, or a lightweight on-demand `telegram.Bot` (delivery only)."""
        app = self._apps.get(account_id)
        if app is not None and app.running:
            yield app.bot
            return
        from telegram import Bot

        async with Bot(token) as bot:
            yield bot

    @staticmethod
    async def _account_for(session: AsyncSession, msg: OutboxMessage) -> MessagingAccount | None:
        """The bot that delivers `msg`: its `account_id` when that account is still active, else the nearest
        Telegram account of the facility that sent it."""
        account = await repo.get_account(session, msg.account_id) if msg.account_id else None
        if (account is None or not account.is_active) and msg.unit_id:
            account = await units_repo.resolve_account(session, msg.unit_id, "telegram")
        return account if account is not None and account.is_active else None

    async def deliver(self, session: AsyncSession, msg: OutboxMessage) -> bool:
        """Deliver an outbox row (`channel=telegram`, `to`=chat id).

        `result_ready` with `payload.documentId` → the document PDF with the text as caption; other kinds → text.
        Returns True on success; False (with `msg.error` set) when no bot serves the message's unit / the chat
        rejected the message; raises on transient Telegram errors so the dispatcher retries with backoff."""
        from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut

        account = await self._account_for(session, msg)
        token = service.account_token(account)
        if account is None or not token:
            msg.error = NOT_CONFIGURED
            return False
        if msg.account_id is None:
            msg.account_id = account.id  # record which bot carried it
        try:
            async with self._open_bot(account.id, token) as bot:
                sent = await self._send(session, bot, msg)
        except (RetryAfter, TimedOut, NetworkError):
            raise
        except InvalidToken:
            msg.error = "telegram_invalid_token"
            return False
        except Forbidden as exc:  # patient blocked the bot / never started it / chat deleted — permanent
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
            document = await repo.get_document(session, uuid.UUID(str(document_id)))
            if document is not None and (msg.patient_id is None or document.patient_id == msg.patient_id):
                pdf = await service.document_pdf(session, document)
                return await bot.send_document(chat_id=chat_id, document=pdf, filename=service.pdf_filename(document), caption=(msg.text or "")[:1024])
        return await bot.send_message(chat_id=chat_id, text=msg.text)


bot_manager = BotManager()
