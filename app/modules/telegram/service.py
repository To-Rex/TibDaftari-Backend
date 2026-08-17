"""Telegram module rules: login/link flow, patient views (cheques, payments, results), status.

Handlers (`handlers.py`) and the bot manager call these with a session from `session_scope()`;
this module never touches python-telegram-bot objects, so every rule is unit-testable without a bot.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.config import settings
from app.core.exceptions import RateLimitedError
from app.core.security import random_digits, sha256_hex
from app.core.textutil import fmt_money_ru, is_valid_uz_phone, norm_phone, slugify
from app.core.timeutil import fmt_datetime
from app.infrastructure.db.models import Company, OtpChallenge, Patient, ResultDocument
from app.infrastructure.redis import rate_limit
from app.infrastructure.redis.client import get_redis
from app.modules.messaging import service as messaging
from app.modules.telegram import repository as repo
from app.modules.telegram.schemas import TelegramStatusOut
from app.modules.telegram.texts import DEFAULT_LANG, norm_lang, t

log = logging.getLogger("telegram")

ORDERS_LIMIT = 10
PAYMENTS_LIMIT = 10
RESULTS_LIMIT = 20
TELEGRAM_TEXT_LIMIT = 4000
OTP_PURPOSE = "telegram"

# ----------------------------------------------------------------------------- pure helpers


def parse_phone(raw: str | None) -> str | None:
    """Text or contact phone → normalised `998XXXXXXXXX`, or None when it is not a valid Uzbek number.

    Like the legacy bot, a longer string whose last 9 digits form a local number is accepted."""
    phone = norm_phone(raw)
    if is_valid_uz_phone(phone):
        return phone
    d = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(d) > 9:
        tail = "998" + d[-9:]
        if is_valid_uz_phone(tail):
            return tail
    return None


def display_name(patient: Patient | None, lang: str) -> str:
    """Patient full name, or the localised placeholder."""
    return (patient.full_name if patient and patient.full_name else "") or t(lang, "default_user")


def clip(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Telegram messages are capped at 4096 chars — trim with an ellipsis like the legacy bot."""
    return text if len(text) <= limit else text[:limit] + "\n..."


def format_orders(lang: str, rows: list[tuple[object, list[str]]]) -> str:
    """"My cheques" message body: number, date, services bullets, sum, paid status."""
    if not rows:
        return t(lang, "no_cheks")
    parts = [t(lang, "cheks_header", n=len(rows))]
    for order, services in rows:
        status = t(lang, "paid") if order.payment == "paid" else t(lang, "unpaid")  # type: ignore[attr-defined]
        block = f"{t(lang, 'chek_no')} {order.number or '—'} — {fmt_datetime(order.created_at)}\n"  # type: ignore[attr-defined]
        for name in services:
            block += f"   • {name}\n"
        block += f"   {t(lang, 'sum_label')}: {fmt_money_ru(order.total)} {t(lang, 'som')}\n"  # type: ignore[attr-defined]
        block += f"   {t(lang, 'status_label')}: {status}\n"
        parts.append(block)
    return clip("\n".join(parts))


def format_payments(lang: str, rows: list[tuple[object, str]], total_count: int, total_sum: int) -> str:
    """"My payments" message body: last payments + grand total."""
    if not rows:
        return t(lang, "no_payments")
    parts = [t(lang, "payments_header", n=len(rows))]
    for payment, number in rows:
        parts.append(f"✅ {fmt_datetime(payment.created_at)} — {fmt_money_ru(payment.amount)} {t(lang, 'som')} ({t(lang, 'chek_no')} {number or '—'})")  # type: ignore[attr-defined]
    parts.append(t(lang, "payments_total", n=total_count, sum=fmt_money_ru(total_sum)))
    return clip("\n".join(parts))


def result_caption(lang: str, order_number: str | None, title: str | None) -> str:
    """Caption of a result PDF sent to the patient."""
    return f"🧾 {t(lang, 'chek_no')} {order_number or '—'} — {title or t(lang, 'result_default')}"


def pdf_filename(document: ResultDocument) -> str:
    """Stable, ASCII-safe file name for a document PDF."""
    return f"{slugify(document.title)}.pdf"


def webhook_secret_ok(company: Company | None, secret: str) -> bool:
    """True when the company configured `settings.telegramWebhookSecret` and it matches (constant time)."""
    if not company:
        return False
    configured = str((company.settings or {}).get("telegramWebhookSecret") or "")
    return bool(configured) and hmac.compare_digest(configured, secret)


# ----------------------------------------------------------------------------- language


async def chat_lang(session: AsyncSession, company_id: uuid.UUID, chat_id: str) -> str:
    """Language of a chat (default uz)."""
    return norm_lang(await repo.get_lang(session, company_id, chat_id))


async def set_chat_lang(session: AsyncSession, company_id: uuid.UUID, chat_id: str, lang: str) -> str:
    """Persist the chosen language for the chat; returns the effective language."""
    lang = norm_lang(lang)
    await repo.set_lang(session, company_id, chat_id, lang)
    return lang


# ----------------------------------------------------------------------------- login / link flow


@dataclass(slots=True)
class LoginStart:
    """Outcome of the phone step: `status` ∈ bad_phone | not_found | too_many | otp_sent."""

    status: str
    challenge_id: uuid.UUID | None = None
    phone: str | None = None


@dataclass(slots=True)
class VerifyResult:
    """Outcome of the code step: `status` ∈ linked | wrong | over | expired."""

    status: str
    attempts_left: int = 0
    patient_name: str = ""
    extra: dict[str, object] = field(default_factory=dict)


async def begin_login(session: AsyncSession, company: Company, chat_id: str, raw_phone: str | None, lang: str) -> LoginStart:
    """Phone step: validate → find the patient in THIS company → create an OTP challenge → enqueue the SMS."""
    phone = parse_phone(raw_phone)
    if not phone:
        return LoginStart(status="bad_phone")
    patient = await repo.patient_by_phone(session, company.id, phone)
    if not patient:
        return LoginStart(status="not_found")
    if not await _otp_send_allowed(phone, chat_id):
        return LoginStart(status="too_many")
    code = random_digits(settings.otp_length)
    challenge = OtpChallenge(
        phone=phone,
        purpose=OTP_PURPOSE,
        code_hash=sha256_hex(code),
        max_attempts=settings.otp_max_attempts,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.otp_ttl_seconds),
        company_id=company.id,
        meta={"chatId": chat_id, "patientId": str(patient.id), "lang": lang},
    )
    session.add(challenge)
    await session.flush()
    await messaging.enqueue_sms_if_configured(
        session, company, kind="otp", to=phone, text=t(lang, "otp_sms", otp=code), patient_id=patient.id
    )
    if settings.otp_dev_mode:
        log.info("telegram OTP for %s (company %s, chat %s): %s (dev mode)", phone, company.id, chat_id, code)
    try:
        await get_redis().set(f"otp:cooldown:{phone}", "1", ex=settings.otp_resend_cooldown_seconds)
    except Exception:  # pragma: no cover - Redis degraded
        pass
    return LoginStart(status="otp_sent", challenge_id=challenge.id, phone=phone)


async def _otp_send_allowed(phone: str, chat_id: str) -> bool:
    """Same limits as the HTTP OTP flow: 5/hour per phone, 60s resend cooldown (shared key), 10/hour per chat."""
    try:
        if await get_redis().exists(f"otp:cooldown:{phone}"):
            return False
    except Exception:  # pragma: no cover - Redis degraded
        pass
    try:
        await rate_limit.enforce(f"rl:otp:phone:{phone}", 5, 3600)
        await rate_limit.enforce(f"rl:otp:tg:{chat_id}", 10, 3600)
    except RateLimitedError:
        return False
    return True


async def verify_code(session: AsyncSession, company_id: uuid.UUID, chat_id: str, challenge_id: uuid.UUID | None, code: str, lang: str) -> VerifyResult:
    """Code step: check the challenge (max attempts, expiry), then link the oldest patient with that phone."""
    ch = await repo.get_challenge(session, challenge_id) if challenge_id else None
    now = datetime.now(UTC)
    if not ch or ch.purpose != OTP_PURPOSE or ch.company_id != company_id or ch.consumed_at or ch.expires_at <= now:
        return VerifyResult(status="expired")
    if (ch.meta or {}).get("chatId") != chat_id:
        return VerifyResult(status="expired")
    if sha256_hex(code.strip()) != ch.code_hash:
        ch.attempts = (ch.attempts or 0) + 1
        left = max(0, ch.max_attempts - ch.attempts)
        if left == 0:
            ch.consumed_at = now
            return VerifyResult(status="over")
        return VerifyResult(status="wrong", attempts_left=left)
    ch.consumed_at = now
    patient = await repo.patient_by_phone(session, company_id, ch.phone)
    if not patient:
        return VerifyResult(status="expired")
    await repo.unlink_chat(session, company_id, chat_id, now)  # one chat ↔ one patient per company
    link = await repo.link_patient(session, patient, chat_id, lang, now)
    await audit(
        session,
        actor_type="patient",
        actor_id=patient.id,
        company_id=company_id,
        action="telegram_link",
        entity="patient",
        entity_id=patient.id,
        after={"chatId": chat_id, "linkId": str(link.id), "lang": lang},
    )
    return VerifyResult(status="linked", patient_name=display_name(patient, lang))


async def logout(session: AsyncSession, company_id: uuid.UUID, chat_id: str, lang: str) -> str | None:
    """Unlink the chat from its patient. Returns the patient's display name, or None when nothing was linked."""
    patient = await repo.linked_patient(session, company_id, chat_id)
    if not patient:
        return None
    name = display_name(patient, lang)
    now = datetime.now(UTC)
    ids = await repo.unlink_chat(session, company_id, chat_id, now)
    await audit(
        session,
        actor_type="patient",
        actor_id=patient.id,
        company_id=company_id,
        action="telegram_unlink",
        entity="patient",
        entity_id=patient.id,
        before={"chatId": chat_id, "patients": [str(i) for i in ids]},
    )
    return name


# ----------------------------------------------------------------------------- patient views


async def orders_text(session: AsyncSession, company_id: uuid.UUID, patient_id: uuid.UUID, lang: str) -> str:
    """Localised "my cheques" message (last 10 orders)."""
    rows = await repo.last_orders(session, company_id, patient_id, ORDERS_LIMIT)
    return format_orders(lang, rows)


async def payments_text(session: AsyncSession, company_id: uuid.UUID, patient_id: uuid.UUID, lang: str) -> str:
    """Localised "my payments" message (last 10 payments + totals)."""
    rows = await repo.last_payments(session, company_id, patient_id, PAYMENTS_LIMIT)
    if not rows:
        return format_payments(lang, [], 0, 0)
    count, total = await repo.payments_total(session, company_id, patient_id)
    return format_payments(lang, rows, count, total)


async def document_pdf(session: AsyncSession, document: ResultDocument) -> bytes:
    """PDF bytes of a document via the orders service (stores the file); falls back to a plain snapshot render."""
    from app.modules.orders import service as orders_svc

    ensure = getattr(orders_svc, "ensure_document_pdf", None)
    if ensure is not None:
        return await ensure(session, document)
    from app.modules.templates import service as templates_svc

    return await templates_svc.render_snapshot_pdf(session, document.snapshot or {})


@dataclass(slots=True)
class ResultFile:
    data: bytes
    filename: str
    caption: str


async def result_files(session: AsyncSession, company_id: uuid.UUID, patient_id: uuid.UUID, lang: str) -> list[ResultFile]:
    """Newest 20 final result documents rendered to PDF (a document that fails to render is skipped + logged)."""
    rows = await repo.latest_final_documents(session, company_id, patient_id, RESULTS_LIMIT)
    out: list[ResultFile] = []
    for doc, number in rows:
        try:
            data = await document_pdf(session, doc)
        except Exception:
            log.exception("telegram: result PDF %s could not be rendered", doc.id)
            continue
        out.append(ResultFile(data=data, filename=pdf_filename(doc), caption=result_caption(lang, number, doc.title)))
    return out


# ----------------------------------------------------------------------------- staff API


def status_out(company: Company, running: bool) -> TelegramStatusOut:
    """`GET /companies/{id}/telegram/status` projection."""
    return TelegramStatusOut(connected=bool(company.telegram_bot_token_enc), bot_username=company.telegram_bot_username, running=running)


def default_lang_for(company: Company | None) -> str:
    """Language used before a chat picked one: the company locale when it is a bot language, else uz."""
    return norm_lang(company.locale) if company and company.locale in ("uz", "ru", "en") else DEFAULT_LANG
