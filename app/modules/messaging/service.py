"""Messaging service — the ONLY way other modules create SMS/Telegram messages and notifications.

`enqueue()` writes an outbox row; the dispatcher worker (`dispatcher.py`) delivers it
(Xabarchi for SMS, the company bot for Telegram). Nothing is sent synchronously inside a
request — a slow provider must never slow down the clinic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.textutil import fmt_money_ru, is_valid_uz_phone, norm_phone
from app.infrastructure.db.models import Company, Notification, OutboxMessage

# ----------------------------------------------------------------------------- texts

DEFAULT_TEMPLATES: dict[str, str] = {
    # {patient} {order} {service} {company} {amount} {count} {code}
    "payment_receipt": "Chek {order}: {amount} so‘m qabul qilindi. Natijalar tayyor bo‘lganda xabar beramiz. {company}",
    "result_ready": "{service} natijasi tayyor. Portalda ko‘rishingiz mumkin. {company}",
    "result_ready_order": "{service}: {count} ta tahlil natijasi tayyor. Portalda ko‘rishingiz mumkin. {company}",
    "reminder": "Hurmatli {patient}! Sizni {company} klinikasida kutamiz. Chek: {order}",
    "otp": "Sizning tasdiqlash kodingiz: {code}",
}


def render_text(company: Company | None, kind: str, **vars: Any) -> str:
    """Company override (companies.settings.smsTemplates[kind]) or the default text."""
    overrides = ((company.settings or {}).get("smsTemplates") or {}) if company else {}
    tpl = overrides.get(kind) or DEFAULT_TEMPLATES.get(kind, "{service}")
    values = {"patient": "", "order": "", "service": "", "company": company.name if company else "", "amount": "", "count": "", "code": ""}
    values.update({k: ("" if v is None else str(v)) for k, v in vars.items()})
    out = tpl
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    return out


def payment_receipt_text(company: Company, order_number: str, amount: int, patient_name: str = "") -> str:
    return render_text(company, "payment_receipt", order=order_number, amount=fmt_money_ru(amount), patient=patient_name)


def result_ready_text(company: Company, service_name: str, patient_name: str = "", order_number: str = "") -> str:
    return render_text(company, "result_ready", service=service_name, patient=patient_name, order=order_number)


def result_ready_order_text(company: Company, template_name: str, count: int, patient_name: str = "", order_number: str = "") -> str:
    return render_text(company, "result_ready_order", service=template_name, count=count, patient=patient_name, order=order_number)


def otp_text(company: Company | None, code: str) -> str:
    return render_text(company, "otp", code=code)


# ----------------------------------------------------------------------------- outbox


async def enqueue(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    channel: str,
    kind: str,
    to: str,
    text: str,
    branch_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    scheduled_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
    created_by: uuid.UUID | None = None,
) -> OutboxMessage:
    now = datetime.now(UTC)
    if channel == "sms":
        to = norm_phone(to)
    scheduled = scheduled_at if scheduled_at and scheduled_at > now else None
    msg = OutboxMessage(
        company_id=company_id,
        branch_id=branch_id,
        patient_id=patient_id,
        order_id=order_id,
        document_id=document_id,
        channel=channel,
        kind=kind,
        to=to,
        text=text,
        status="scheduled" if scheduled else "queued",
        scheduled_at=scheduled_at,
        next_attempt_at=scheduled or now,
        attempts=0,
        payload=payload or {},
        created_by=created_by,
    )
    session.add(msg)
    await session.flush()
    return msg


async def enqueue_sms_if_configured(session: AsyncSession, company: Company, **kwargs: Any) -> OutboxMessage | None:
    """SMS only makes sense when the company has a provider; else the message is recorded as failed
    (visible in the outbox so staff can see what was not delivered and why)."""
    to = norm_phone(kwargs.get("to", ""))
    if not is_valid_uz_phone(to):
        return None
    msg = await enqueue(session, company_id=company.id, channel="sms", **{**kwargs, "to": to})
    if company.sms_provider == "none" or not company.sms_api_key_enc:
        msg.status = "failed"
        msg.error = "sms_not_configured"
        msg.next_attempt_at = None
    return msg


# ----------------------------------------------------------------------------- notifications


async def notify(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    title: str,
    body: str,
    kind: str = "info",
    link: str | None = None,
    employee_id: uuid.UUID | None = None,
) -> Notification:
    n = Notification(company_id=company_id, employee_id=employee_id, title=title, body=body, kind=kind, link=link, read_by=[])
    session.add(n)
    await session.flush()
    return n


async def count_queued(session: AsyncSession, company_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(OutboxMessage).where(
                OutboxMessage.company_id == company_id, OutboxMessage.deleted_at.is_(None), OutboxMessage.status.in_(("queued", "scheduled"))
            )
        )
    ).scalar_one()


async def mark_document_delivery(session: AsyncSession, document_id: uuid.UUID, channel: str, status: str, detail: str | None = None) -> None:
    """Update the matching delivery entry inside result_documents.deliveries (JSONB)."""
    from app.infrastructure.db.models import ResultDocument

    doc = await session.get(ResultDocument, document_id)
    if not doc:
        return
    deliveries = list(doc.deliveries or [])
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(UTC).microsecond // 1000:03d}Z"
    found = False
    for d in deliveries:
        if d.get("channel") == channel:
            d.update({"status": status, "at": now, **({"detail": detail} if detail else {})})
            found = True
    if not found:
        deliveries.append({"channel": channel, "status": status, "at": now, **({"detail": detail} if detail else {})})
    await session.execute(update(ResultDocument).where(ResultDocument.id == document_id).values(deliveries=deliveries))
