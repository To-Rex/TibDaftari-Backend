"""SQL helpers for the Telegram module (chat prefs, links, patient lookups, bot-facing reads).

Every query is scoped by `company_id` — one bot serves exactly one clinic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    Company,
    Order,
    OrderItem,
    OtpChallenge,
    Patient,
    Payment,
    ResultDocument,
    TelegramChatPref,
    TelegramLink,
)

# ----------------------------------------------------------------------------- companies


async def companies_with_bot(session: AsyncSession) -> list[Company]:
    """Alive, active companies that have a Telegram bot token."""
    stmt = select(Company).where(alive(Company), Company.is_active.is_(True), Company.telegram_bot_token_enc.is_not(None))
    return list((await session.execute(stmt)).scalars().all())


async def get_company(session: AsyncSession, company_id: uuid.UUID) -> Company | None:
    """Alive company by id (None when missing / soft-deleted)."""
    c = await session.get(Company, company_id)
    return c if c and c.deleted_at is None else None


# ----------------------------------------------------------------------------- chat prefs


async def get_lang(session: AsyncSession, company_id: uuid.UUID, chat_id: str) -> str | None:
    """Stored language of a chat (None when the chat never picked one)."""
    pref = await session.get(TelegramChatPref, (company_id, chat_id))
    return pref.lang if pref else None


async def set_lang(session: AsyncSession, company_id: uuid.UUID, chat_id: str, lang: str) -> None:
    """Upsert the chat language."""
    pref = await session.get(TelegramChatPref, (company_id, chat_id))
    if pref:
        pref.lang = lang
        pref.updated_at = datetime.now(UTC)
    else:
        session.add(TelegramChatPref(company_id=company_id, chat_id=chat_id, lang=lang))
    await session.flush()


# ----------------------------------------------------------------------------- patients / links


async def linked_patient(session: AsyncSession, company_id: uuid.UUID, chat_id: str) -> Patient | None:
    """The patient of this company currently linked to `chat_id` (oldest when several)."""
    stmt = (
        select(Patient)
        .where(Patient.company_id == company_id, Patient.telegram_chat_id == chat_id, alive(Patient))
        .order_by(Patient.created_at.asc(), Patient.id.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def patient_by_phone(session: AsyncSession, company_id: uuid.UUID, phone: str) -> Patient | None:
    """Oldest alive patient of the company with this normalised phone."""
    stmt = (
        select(Patient)
        .where(Patient.company_id == company_id, Patient.phone == phone, alive(Patient))
        .order_by(Patient.created_at.asc(), Patient.id.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def unlink_chat(session: AsyncSession, company_id: uuid.UUID, chat_id: str, now: datetime) -> list[uuid.UUID]:
    """Detach `chat_id` from every patient of the company; closes open link rows. Returns affected patient ids."""
    rows = (
        await session.execute(
            update(Patient)
            .where(Patient.company_id == company_id, Patient.telegram_chat_id == chat_id, alive(Patient))
            .values(telegram_chat_id=None, updated_at=now)
            .returning(Patient.id)
        )
    ).scalars().all()
    await session.execute(
        update(TelegramLink)
        .where(TelegramLink.company_id == company_id, TelegramLink.chat_id == chat_id, TelegramLink.unlinked_at.is_(None))
        .values(unlinked_at=now)
    )
    return list(rows)


async def link_patient(session: AsyncSession, patient: Patient, chat_id: str, lang: str, now: datetime) -> TelegramLink:
    """Bind the chat to `patient` (portal linked) and record the link row."""
    patient.telegram_chat_id = chat_id
    patient.portal_linked = True
    patient.updated_at = now
    link = TelegramLink(company_id=patient.company_id, patient_id=patient.id, chat_id=chat_id, lang=lang, linked_at=now)
    session.add(link)
    await session.flush()
    return link


async def get_challenge(session: AsyncSession, challenge_id: uuid.UUID) -> OtpChallenge | None:
    """OTP challenge by id."""
    return await session.get(OtpChallenge, challenge_id)


# ----------------------------------------------------------------------------- bot-facing reads


async def last_orders(session: AsyncSession, company_id: uuid.UUID, patient_id: uuid.UUID, limit: int) -> list[tuple[Order, list[str]]]:
    """Newest `limit` orders of the patient with their service names (two queries, no N+1)."""
    stmt = (
        select(Order)
        .where(Order.company_id == company_id, Order.patient_id == patient_id, alive(Order))
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    orders = list((await session.execute(stmt)).scalars().all())
    if not orders:
        return []
    ids = [o.id for o in orders]
    items = (
        await session.execute(
            select(OrderItem.order_id, OrderItem.service_name)
            .where(OrderItem.company_id == company_id, OrderItem.order_id.in_(ids), alive(OrderItem))
            .order_by(OrderItem.created_at.asc())
        )
    ).all()
    names: dict[uuid.UUID, list[str]] = {i: [] for i in ids}
    for order_id, service_name in items:
        names[order_id].append(service_name)
    return [(o, names[o.id]) for o in orders]


async def last_payments(session: AsyncSession, company_id: uuid.UUID, patient_id: uuid.UUID, limit: int) -> list[tuple[Payment, str]]:
    """Newest `limit` non-refunded payments of the patient with the order number."""
    stmt = (
        select(Payment, Order.number)
        .join(Order, Order.id == Payment.order_id)
        .where(Payment.company_id == company_id, Order.patient_id == patient_id, Payment.refunded_at.is_(None), alive(Payment))
        .order_by(Payment.created_at.desc())
        .limit(limit)
    )
    return [(p, n) for p, n in (await session.execute(stmt)).all()]


async def payments_total(session: AsyncSession, company_id: uuid.UUID, patient_id: uuid.UUID) -> tuple[int, int]:
    """(count, sum) of all non-refunded payments of the patient."""
    stmt = (
        select(func.count(), func.coalesce(func.sum(Payment.amount), 0))
        .select_from(Payment)
        .join(Order, Order.id == Payment.order_id)
        .where(Payment.company_id == company_id, Order.patient_id == patient_id, Payment.refunded_at.is_(None), alive(Payment))
    )
    count, total = (await session.execute(stmt)).one()
    return int(count), int(total)


async def latest_final_documents(session: AsyncSession, company_id: uuid.UUID, patient_id: uuid.UUID, limit: int) -> list[tuple[ResultDocument, str]]:
    """Newest `limit` final result documents of the patient with the order number."""
    stmt = (
        select(ResultDocument, Order.number)
        .join(Order, Order.id == ResultDocument.order_id)
        .where(
            ResultDocument.company_id == company_id,
            ResultDocument.patient_id == patient_id,
            ResultDocument.status == "final",
            alive(ResultDocument),
        )
        .order_by(ResultDocument.created_at.desc())
        .limit(limit)
    )
    return [(d, n) for d, n in (await session.execute(stmt)).all()]


async def get_document(session: AsyncSession, company_id: uuid.UUID, document_id: uuid.UUID) -> ResultDocument | None:
    """Alive result document of the company."""
    stmt = select(ResultDocument).where(ResultDocument.id == document_id, ResultDocument.company_id == company_id, alive(ResultDocument))
    return (await session.execute(stmt)).scalar_one_or_none()
