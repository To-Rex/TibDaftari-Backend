"""Messaging service — the ONLY way other modules create SMS/Telegram messages and notifications.

`enqueue()` writes an outbox row; the dispatcher worker (`dispatcher.py`) delivers it
(Xabarchi for SMS, the company bot for Telegram). Nothing is sent synchronously inside a
request — a slow provider must never slow down the clinic.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.crypto import decrypt, encrypt
from app.core.exceptions import NotFoundError, ValidationError
from app.core.pagination import page_of
from app.core.schemas import Page
from app.core.textutil import fmt_money_ru, is_valid_uz_phone, norm_phone
from app.core.timeutil import to_iso
from app.infrastructure.db.models import Company, Notification, OutboxMessage
from app.modules.messaging import repository as repo
from app.modules.messaging.schemas import NotificationOut, OutboxCountsOut, OutboxMessageOut, OutboxQuery, SendIn

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
    tpl = overrides.get(kind)
    if not tpl and kind == "result_ready_order":
        # A customised `result_ready` text also wins for order-scope approvals (DOMAIN_RULES §10).
        tpl = overrides.get("result_ready")
    tpl = tpl or DEFAULT_TEMPLATES.get(kind, "{service}")
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


def build_message(
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
    """Unsaved outbox row: `scheduled` when scheduled_at is in the future, else `queued` and due now."""
    now = datetime.now(UTC)
    if channel == "sms":
        to = norm_phone(to)
    scheduled = scheduled_at if scheduled_at and scheduled_at > now else None
    return OutboxMessage(
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
    """Persist one outbox row (flushed so the id is available); the worker delivers it later."""
    msg = build_message(
        company_id=company_id,
        channel=channel,
        kind=kind,
        to=to,
        text=text,
        branch_id=branch_id,
        patient_id=patient_id,
        order_id=order_id,
        document_id=document_id,
        scheduled_at=scheduled_at,
        payload=payload,
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
    configured = company.sms_provider != "none" and bool(company.sms_api_key_enc)
    if kwargs.get("kind") == "otp":
        # OTP codes must never be readable from the outbox: the visible text is masked and the
        # real text travels encrypted in the payload; nothing is persisted without a provider.
        if not configured:
            return None
        text = str(kwargs.get("text") or "")
        kwargs = {**kwargs, "text": mask_otp_text(text), "payload": {**(kwargs.get("payload") or {}), SECRET_TEXT_KEY: encrypt(text)}}
    msg = await enqueue(session, company_id=company.id, channel="sms", **{**kwargs, "to": to})
    if not configured:
        msg.status = "failed"
        msg.error = "sms_not_configured"
        msg.next_attempt_at = None
    return msg


SECRET_TEXT_KEY = "secretText"


def mask_otp_text(text: str) -> str:
    """Replace every digit run of OTP length or longer with asterisks."""
    return re.sub(r"\d{4,}", "****", text)


def outgoing_text(msg: OutboxMessage) -> str:
    """The text to hand to the provider: the encrypted secret (OTP) when present, else `text`."""
    secret = (msg.payload or {}).get(SECRET_TEXT_KEY)
    if secret:
        return decrypt(str(secret)) or msg.text
    return msg.text


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


# ----------------------------------------------------------------------------- staff API (router → service)


async def list_outbox(session: AsyncSession, company_id: uuid.UUID, q: OutboxQuery) -> Page[OutboxMessageOut]:
    """§8 `listOutbox`: status/kind exact, search on `to` digits or folded text, newest first."""
    rows, total = await repo.list_outbox(session, company_id, q, status=q.status, kind=q.kind)
    return page_of([OutboxMessageOut.model_validate(r) for r in rows], q, total)


async def outbox_counts(session: AsyncSession, company_id: uuid.UUID, q: OutboxQuery) -> OutboxCountsOut:
    """Status-tab counters for the messages page (same kind/search filters as the list)."""
    counts = await repo.outbox_counts(session, company_id, kind=q.kind, search=q.search)
    keys = ("scheduled", "queued", "sending", "sent", "delivered", "failed")
    return OutboxCountsOut(all=sum(counts.values()), **{k: counts.get(k, 0) for k in keys})


async def send(
    session: AsyncSession, company_id: uuid.UUID, staff: StaffPrincipal, body: SendIn, meta: RequestMeta
) -> tuple[list[OutboxMessageOut], list[str]]:
    """§8 `send`: one SMS outbox row per distinct valid recipient; broadcast (>1) needs messaging.broadcast.

    Returns (created messages, invalid recipients that were skipped)."""
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in body.to:
        phone = norm_phone(raw)
        if not is_valid_uz_phone(phone):
            if raw.strip():
                invalid.append(raw.strip())
            continue
        if phone not in seen:
            seen.add(phone)
            valid.append(phone)
    if not valid:
        raise ValidationError("Telefon raqam noto‘g‘ri", code="invalid_phone", details={"invalid": invalid} if invalid else None)
    if len(valid) > 1:
        staff.require("messaging.broadcast")
    company = await session.get(Company, company_id)
    if not company or company.deleted_at is not None:
        raise NotFoundError("Kompaniya topilmadi")
    created = [
        build_message(company_id=company_id, channel="sms", kind=body.kind, to=phone, text=body.text, branch_id=staff.branch_id, scheduled_at=body.scheduled_at, created_by=staff.id)
        for phone in valid
    ]
    session.add_all(created)
    await session.flush()  # one round trip for the whole broadcast
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        company_id=company_id,
        action="send",
        entity="outbox_message",
        entity_id=created[0].id if len(created) == 1 else None,
        after={"kind": body.kind, "recipients": len(created), "invalid": len(invalid), "scheduledAt": to_iso(body.scheduled_at), "text": body.text[:200]},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return [OutboxMessageOut.model_validate(m) for m in created], invalid


async def list_notifications(session: AsyncSession, staff: StaffPrincipal) -> list[NotificationOut]:
    """§8 `notifications`: my company, company-wide or addressed to me, newest 50; `read` = me ∈ read_by."""
    rows = await repo.list_notifications(session, staff.company_id, staff.id)
    return [
        NotificationOut(id=n.id, title=n.title, body=n.body, kind=n.kind, created_at=n.created_at, read=staff.id in (n.read_by or []), link=n.link)
        for n in rows
    ]


async def mark_read(session: AsyncSession, staff: StaffPrincipal, notification_id: uuid.UUID | None) -> None:
    """§8 `markRead(id?)`: one or all visible notifications become read for the caller (idempotent)."""
    await repo.mark_read(session, staff.company_id, staff.id, notification_id)
