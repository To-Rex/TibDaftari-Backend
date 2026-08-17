"""python-telegram-bot handlers — the NavbatApp conversation, one Application per company.

Flow: /start → language (inline) → already linked? welcome back + menu : ask phone (contact button or text)
→ patient lookup in the company → OTP SMS → code (3 attempts) → link → menu.
Menu: 🧾 cheques · 💰 payments · 📄 results (PDFs) · 🚪 logout · 🌐 language. Commands: /lang /logout /cancel.

python-telegram-bot is imported lazily inside `build_application()` so the API boots even when the
package is broken. Every handler runs its DB work in a short `session_scope()` transaction and swallows
its own errors (logged) — a bot bug must never take the process down.
"""

from __future__ import annotations

import logging
import uuid
import warnings
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.infrastructure.db.session import session_scope
from app.modules.telegram import repository as repo
from app.modules.telegram import service
from app.modules.telegram.texts import COMMANDS, LANGS, PICK_LANG, button_regex, norm_lang, t

if TYPE_CHECKING:  # pragma: no cover
    from telegram.ext import Application

log = logging.getLogger("telegram")

WAITING_FOR_LANG, WAITING_FOR_PHONE, WAITING_FOR_OTP = range(3)
UD_CHALLENGE = "otpChallengeId"
UD_LANG = "lang"


def _lang_keyboard() -> Any:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang:uz"), InlineKeyboardButton("🇺🇿 Ўзбекча (кирилл)", callback_data="lang:uzc")],
            [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru"), InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")],
        ]
    )


def main_keyboard(lang: str) -> Any:
    """Reply keyboard of the main menu in `lang` (same layout as NavbatApp)."""
    from telegram import ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        [[t(lang, "btn_cheks"), t(lang, "btn_payments")], [t(lang, "btn_results"), t(lang, "btn_logout")], [t(lang, "btn_lang")]],
        resize_keyboard=True,
    )


def _phone_keyboard(lang: str) -> Any:
    from telegram import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup([[KeyboardButton(t(lang, "btn_share_phone"), request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)


def build_application(token: str, company_id: uuid.UUID) -> Application:
    """Create the PTB Application for one company with all handlers registered (not started)."""
    from telegram import BotCommand, ReplyKeyboardRemove, Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        ConversationHandler,
        MessageHandler,
        filters,
    )
    from telegram.warnings import PTBUserWarning

    Ctx = ContextTypes.DEFAULT_TYPE  # noqa: N806 - type alias

    # ------------------------------------------------------------------ helpers

    def _chat_id(update: Update) -> str:
        return str(update.effective_chat.id) if update.effective_chat else ""

    async def _lang(update: Update, context: Ctx) -> str:
        """Chat language: cached in user_data, loaded from telegram_chat_prefs once."""
        cached = context.user_data.get(UD_LANG) if context.user_data is not None else None
        if cached:
            return str(cached)
        async with session_scope() as s:
            stored = await repo.get_lang(s, company_id, _chat_id(update))
            lang = norm_lang(stored) if stored else service.default_lang_for(await repo.get_company(s, company_id))
        if context.user_data is not None:
            context.user_data[UD_LANG] = lang
        return lang

    async def _set_lang(update: Update, context: Ctx, lang: str) -> str:
        async with session_scope() as s:
            lang = await service.set_chat_lang(s, company_id, _chat_id(update), lang)
        if context.user_data is not None:
            context.user_data[UD_LANG] = lang
        return lang

    async def _linked(update: Update) -> Any:
        async with session_scope() as s:
            return await repo.linked_patient(s, company_id, _chat_id(update))

    async def _reply(update: Update, text: str, reply_markup: Any = None) -> None:
        if update.effective_chat:
            await update.effective_chat.send_message(text, reply_markup=reply_markup)

    def guarded(fn: Callable[[Update, Any], Awaitable[Any]], fallback: Any = None) -> Callable[[Update, Any], Awaitable[Any]]:
        """Never let a handler exception escape: log, tell the user, return `fallback` (conversation state)."""

        async def _wrapped(update: Update, context: Ctx) -> Any:
            try:
                return await fn(update, context)
            except Exception:
                log.exception("telegram[%s]: handler %s failed", company_id, fn.__name__)
                try:
                    await _reply(update, t(context.user_data.get(UD_LANG) if context.user_data else None, "error"))
                except Exception:
                    pass
                return fallback

        _wrapped.__name__ = fn.__name__
        return _wrapped

    # ------------------------------------------------------------------ conversation: /start → lang → phone → otp

    async def start(update: Update, context: Ctx) -> int:
        if context.user_data is not None:
            context.user_data.pop(UD_CHALLENGE, None)
        await _reply(update, PICK_LANG, _lang_keyboard())
        return WAITING_FOR_LANG

    async def _apply_lang_choice(update: Update, context: Ctx) -> str:
        q = update.callback_query
        lang = await _set_lang(update, context, (q.data or "").split(":", 1)[-1])
        await q.answer()
        try:
            await q.edit_message_text(t(lang, "lang_set"))
        except Exception:
            pass
        return lang

    async def lang_chosen(update: Update, context: Ctx) -> int:
        lang = await _apply_lang_choice(update, context)
        patient = await _linked(update)
        if patient:
            await _reply(update, t(lang, "welcome_back", name=service.display_name(patient, lang)), main_keyboard(lang))
            return ConversationHandler.END
        await _reply(update, t(lang, "ask_phone"), _phone_keyboard(lang))
        return WAITING_FOR_PHONE

    async def phone_received(update: Update, context: Ctx) -> int:
        lang = await _lang(update, context)
        msg = update.message
        raw = msg.contact.phone_number if msg and msg.contact else (msg.text if msg else None)
        async with session_scope() as s:
            company = await repo.get_company(s, company_id)
            if not company:
                await _reply(update, t(lang, "error"))
                return ConversationHandler.END
            result = await service.begin_login(s, company, _chat_id(update), raw, lang)
        if result.status == "bad_phone":
            await _reply(update, t(lang, "bad_phone"), _phone_keyboard(lang))
            return WAITING_FOR_PHONE
        if result.status == "not_found":
            await _reply(update, t(lang, "not_found"), _phone_keyboard(lang))
            return WAITING_FOR_PHONE
        if result.status == "too_many":
            await _reply(update, t(lang, "too_many"), _phone_keyboard(lang))
            return WAITING_FOR_PHONE
        if context.user_data is not None:
            context.user_data[UD_CHALLENGE] = str(result.challenge_id)
        await _reply(update, t(lang, "otp_sent", n=settings.otp_length), ReplyKeyboardRemove())
        return WAITING_FOR_OTP

    async def otp_received(update: Update, context: Ctx) -> int:
        lang = await _lang(update, context)
        raw = context.user_data.get(UD_CHALLENGE) if context.user_data is not None else None
        challenge_id = uuid.UUID(str(raw)) if raw else None
        code = (update.message.text if update.message else "") or ""
        async with session_scope() as s:
            result = await service.verify_code(s, company_id, _chat_id(update), challenge_id, code, lang)
        if result.status == "wrong":
            await _reply(update, t(lang, "wrong_code", attempts=result.attempts_left))
            return WAITING_FOR_OTP
        if context.user_data is not None:
            context.user_data.pop(UD_CHALLENGE, None)
        if result.status == "linked":
            await _reply(update, t(lang, "verified", name=result.patient_name), main_keyboard(lang))
        elif result.status == "over":
            await _reply(update, t(lang, "attempts_over"), ReplyKeyboardRemove())
        else:
            await _reply(update, t(lang, "otp_expired"), ReplyKeyboardRemove())
        return ConversationHandler.END

    async def cancel(update: Update, context: Ctx) -> int:
        lang = await _lang(update, context)
        if context.user_data is not None:
            context.user_data.pop(UD_CHALLENGE, None)
        await _reply(update, t(lang, "cancelled"), ReplyKeyboardRemove())
        return ConversationHandler.END

    # ------------------------------------------------------------------ menu

    async def send_lang_picker(update: Update, context: Ctx) -> None:
        await _reply(update, PICK_LANG, _lang_keyboard())

    async def lang_callback(update: Update, context: Ctx) -> None:
        lang = await _apply_lang_choice(update, context)
        if await _linked(update):
            await _reply(update, t(lang, "kb_updated"), main_keyboard(lang))
        else:
            await _reply(update, t(lang, "not_logged_in"))

    async def logout(update: Update, context: Ctx) -> int:
        lang = await _lang(update, context)
        if context.user_data is not None:
            context.user_data.pop(UD_CHALLENGE, None)
        async with session_scope() as s:
            name = await service.logout(s, company_id, _chat_id(update), lang)
        if name:
            await _reply(update, t(lang, "logged_out", name=name), ReplyKeyboardRemove())
        else:
            await _reply(update, t(lang, "not_logged_in"), ReplyKeyboardRemove())
        return ConversationHandler.END

    async def _require_patient(update: Update, lang: str) -> Any:
        patient = await _linked(update)
        if not patient:
            await _reply(update, t(lang, "need_login"))
        return patient

    async def my_cheks(update: Update, context: Ctx) -> None:
        lang = await _lang(update, context)
        patient = await _require_patient(update, lang)
        if not patient:
            return
        async with session_scope() as s:
            text = await service.orders_text(s, company_id, patient.id, lang)
        await _reply(update, text, main_keyboard(lang))

    async def my_payments(update: Update, context: Ctx) -> None:
        lang = await _lang(update, context)
        patient = await _require_patient(update, lang)
        if not patient:
            return
        async with session_scope() as s:
            text = await service.payments_text(s, company_id, patient.id, lang)
        await _reply(update, text, main_keyboard(lang))

    async def my_results(update: Update, context: Ctx) -> None:
        lang = await _lang(update, context)
        patient = await _require_patient(update, lang)
        if not patient:
            return
        await _reply(update, t(lang, "results_wait"))
        async with session_scope() as s:
            files = await service.result_files(s, company_id, patient.id, lang)
        if not files:
            await _reply(update, t(lang, "no_results"), main_keyboard(lang))
            return
        sent = 0
        for f in files:
            try:
                await update.effective_chat.send_document(document=f.data, filename=f.filename, caption=f.caption)
                sent += 1
            except Exception:
                log.exception("telegram[%s]: result document not sent (%s)", company_id, f.filename)
        await _reply(update, t(lang, "results_done", n=sent), main_keyboard(lang))

    async def fallback_text(update: Update, context: Ctx) -> None:
        """Unknown text: linked → redraw the menu, else ask to sign in (the legacy echo was a leftover)."""
        lang = await _lang(update, context)
        if await _linked(update):
            await _reply(update, t(lang, "kb_updated"), main_keyboard(lang))
        else:
            await _reply(update, t(lang, "need_login"))

    async def on_error(update: object, context: Ctx) -> None:
        log.error("telegram[%s]: unhandled error: %s", company_id, context.error)

    async def post_init(application: Application) -> None:
        for lang_code, cmds in COMMANDS.items():
            try:
                await application.bot.set_my_commands([BotCommand(c, d) for c, d in cmds], language_code=lang_code)
            except Exception as exc:
                log.warning("telegram[%s]: set_my_commands(%s) failed: %s", company_id, lang_code, exc)

    # ------------------------------------------------------------------ wiring

    with warnings.catch_warnings():
        # Mixed callback + text states → PTB warns about per_message=False; that is intentional (legacy parity).
        warnings.filterwarnings("ignore", category=PTBUserWarning, message=".*per_message=False.*")
        conversation = ConversationHandler(
            entry_points=[CommandHandler("start", guarded(start, ConversationHandler.END))],
            states={
                WAITING_FOR_LANG: [CallbackQueryHandler(guarded(lang_chosen, ConversationHandler.END), pattern="^lang:")],
                WAITING_FOR_PHONE: [MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, guarded(phone_received, WAITING_FOR_PHONE))],
                WAITING_FOR_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, guarded(otp_received, WAITING_FOR_OTP))],
            },
            fallbacks=[CommandHandler("cancel", guarded(cancel, ConversationHandler.END)), CommandHandler("logout", guarded(logout, ConversationHandler.END))],
        )

    application = Application.builder().token(token).connect_timeout(10).read_timeout(30).post_init(post_init).build()
    application.add_handler(conversation)
    application.add_handler(CommandHandler("logout", guarded(logout)))
    application.add_handler(CommandHandler("lang", guarded(send_lang_picker)))
    application.add_handler(CallbackQueryHandler(guarded(lang_callback), pattern="^lang:(" + "|".join(LANGS) + ")$"))
    application.add_handler(MessageHandler(filters.Regex(button_regex("btn_cheks")), guarded(my_cheks)))
    application.add_handler(MessageHandler(filters.Regex(button_regex("btn_payments")), guarded(my_payments)))
    application.add_handler(MessageHandler(filters.Regex(button_regex("btn_results")), guarded(my_results)))
    application.add_handler(MessageHandler(filters.Regex(button_regex("btn_logout")), guarded(logout)))
    application.add_handler(MessageHandler(filters.Regex(button_regex("btn_lang")), guarded(send_lang_picker)))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guarded(fallback_text)))
    application.add_error_handler(on_error)
    return application
