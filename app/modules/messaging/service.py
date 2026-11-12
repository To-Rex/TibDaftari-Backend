"""Messaging service — the ONLY way other modules create SMS/Telegram messages and notifications.

`enqueue()` writes an outbox row; the dispatcher worker (`dispatcher.py`) delivers it (Xabarchi for SMS, the
unit's Telegram bot account for Telegram). Nothing is sent synchronously inside a request — a slow provider must
never slow down the clinic.

Credentials and texts are inherited down the organisation tree: `sms_account_for(unit)` / `sms_texts_for(unit)`
pick the nearest `messaging_accounts` / `sms_templates` rows on the path above the facility.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.crypto import decrypt, encrypt
from app.core.exceptions import ValidationError
from app.core.pagination import page_of
from app.core.schemas import Page
from app.core.scope import UnitScope, ids_of, narrow_to
from app.core.textutil import fmt_money_ru, is_valid_uz_phone, norm_phone
from app.core.timeutil import to_iso
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Notification, OutboxMessage, Patient
from app.modules.messaging import repository as repo
from app.modules.messaging.eskiz import DELIVERED_STATES, FAILED_STATES
from app.modules.messaging.providers import SmsAccount, account_to_sms
from app.modules.messaging.schemas import NotificationOut, OutboxCountsOut, OutboxMessageOut, OutboxQuery, SendIn
from app.modules.units import repository as units_repo
from app.modules.units import service as units

#: `OutboxMessage.error` of a row that could not go out because no SMS account is configured for its facility
SMS_NOT_CONFIGURED = "sms_not_configured"  # error code the UI maps to a label (the message text is "SMS provayder sozlanmagan")

# ----------------------------------------------------------------------------- texts

#: every placeholder a text may use; `{company}` / `{branch}` are kept as aliases of `{org}` / `{unit}` so texts
#: saved before the organisation tree keep rendering
PLACEHOLDERS: tuple[str, ...] = ("patient", "order", "service", "org", "unit", "company", "branch", "amount", "total", "remaining", "count", "code", "link", "login", "password")

DEFAULT_TEMPLATES: dict[str, str] = {
    # {org} = the organisation (nearest node above facility level: "Xorazm SES"), {unit} = the facility's name
    # payment receipt: {amount} = this payment, {total} = the cheque total, {remaining} = still to pay (money as 120 000)
    # {link} = the public result PDF (results only); it is what makes the SMS useful on any phone
    "payment_receipt": "Chek {order}: {amount} so‘m qabul qilindi. Natijalar tayyor bo‘lganda xabar beramiz. {org}",
    "result_ready": "{service} natijasi tayyor: {link} {org}",
    "result_ready_order": "{service}: {count} ta tahlil natijasi tayyor: {link} {org}",
    "reminder": "Hurmatli {patient}! Sizni {org} klinikasida kutamiz. Chek: {order}",
    "otp": "Sizning tasdiqlash kodingiz: {code}",
    # portal login issued at reception; {link} = the portal sign-in page
    "portal_access": "{org} shaxsiy kabinetiga kirish: {link} Login: {login} Parol: {password}",
}


def render_text(texts: Mapping[str, str] | None, kind: str, **vars: Any) -> str:
    """The unit's (inherited) text for `kind` — `sms_texts_for()` — or the platform default, with the placeholders
    filled in. `company` and `branch` are aliases of `org` and `unit` (either spelling may be passed or used)."""
    overrides = dict(texts or {})
    tpl = overrides.get(kind)
    if not tpl and kind == "result_ready_order":
        # A customised `result_ready` text also wins for order-scope approvals (DOMAIN_RULES §10).
        tpl = overrides.get("result_ready")
    tpl = tpl or DEFAULT_TEMPLATES.get(kind, "{service}")
    values = dict.fromkeys(PLACEHOLDERS, "")
    values.update({k: ("" if v is None else str(v)) for k, v in vars.items()})
    values["org"] = values["org"] or values["company"]
    values["company"] = values["company"] or values["org"]
    values["unit"] = values["unit"] or values["branch"]
    values["branch"] = values["branch"] or values["unit"]
    out = tpl
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    # an empty value (e.g. no link) must not leave a double space behind (a text written with one stays as is)
    if "  " in out and "  " not in tpl:
        out = re.sub(" {2,}", " ", out).strip()
    return out


async def sms_texts_for(session: AsyncSession, unit_id: uuid.UUID | None) -> dict[str, str]:
    """The SMS texts a message of `unit_id` uses: its own, else the nearest ancestor's, per key (missing = default)."""
    if unit_id is None:
        return {}
    return await units_repo.resolve_sms_texts(session, unit_id)


async def _text_context(session: AsyncSession, unit_id: uuid.UUID | None) -> tuple[dict[str, str], str, str]:
    """`(texts, org, unit)` for a message of the facility `unit_id` — defaults and empty names without one."""
    if unit_id is None:
        return {}, "", ""
    texts = await sms_texts_for(session, unit_id)
    org = await units.org_name(session, unit_id)
    unit = (await units_repo.units_by_ids(session, [unit_id])).get(unit_id)
    unit_name = unit.name if unit is not None and unit.level == "facility" else ""
    return texts, org, unit_name


async def payment_receipt_text(
    session: AsyncSession, unit_id: uuid.UUID | None, order_number: str, amount: int, patient_name: str = "", total: int | None = None, remaining: int | None = None
) -> str:
    texts, org, unit = await _text_context(session, unit_id)
    return render_text(
        texts, "payment_receipt", org=org, unit=unit, order=order_number, amount=fmt_money_ru(amount), patient=patient_name,
        total=fmt_money_ru(total) if total is not None else "", remaining=fmt_money_ru(remaining) if remaining is not None else "",
    )


async def result_ready_text(session: AsyncSession, unit_id: uuid.UUID | None, service_name: str, patient_name: str = "", order_number: str = "", link: str = "") -> str:
    texts, org, unit = await _text_context(session, unit_id)
    return render_text(texts, "result_ready", org=org, unit=unit, service=service_name, patient=patient_name, order=order_number, link=link)


async def result_ready_order_text(
    session: AsyncSession, unit_id: uuid.UUID | None, template_name: str, count: int, patient_name: str = "", order_number: str = "", link: str = ""
) -> str:
    texts, org, unit = await _text_context(session, unit_id)
    return render_text(texts, "result_ready_order", org=org, unit=unit, service=template_name, count=count, patient=patient_name, order=order_number, link=link)


async def otp_text(session: AsyncSession, unit_id: uuid.UUID | None, code: str) -> str:
    texts, org, unit = await _text_context(session, unit_id)
    return render_text(texts, "otp", org=org, unit=unit, code=code)


async def portal_access_text(session: AsyncSession, unit_id: uuid.UUID | None, login: str, password: str, link: str) -> str:
    texts, org, unit = await _text_context(session, unit_id)
    return render_text(texts, "portal_access", org=org, unit=unit, login=login, password=password, link=link)


# ----------------------------------------------------------------------------- SMS accounts


#: re-exported for the callers written against this module (dispatcher, units)
__all__ = ["SmsAccount", "account_to_sms"]


async def sms_account_for(session: AsyncSession, unit_id: uuid.UUID | None) -> SmsAccount | None:
    """The nearest active `sms` account on the unit's path (own first, then ancestors); None = SMS not configured
    here — including when the nearest account says `provider='none'`, which switches SMS off for that subtree."""
    if unit_id is None:
        return None
    return account_to_sms(await units_repo.resolve_account(session, unit_id, "sms"))


# ----------------------------------------------------------------------------- outbox


def build_message(
    *,
    unit_id: uuid.UUID | None,
    channel: str,
    kind: str,
    to: str,
    text: str,
    account_id: uuid.UUID | None = None,
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
        unit_id=unit_id,
        account_id=account_id,
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


async def _enqueue(session: AsyncSession, account: SmsAccount | None, **kwargs: Any) -> OutboxMessage:
    msg = build_message(account_id=account.account_id if account else None, **kwargs)
    session.add(msg)
    await session.flush()
    return msg


async def enqueue(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID | None,
    channel: str,
    kind: str,
    to: str,
    text: str,
    patient_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    scheduled_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
    created_by: uuid.UUID | None = None,
) -> OutboxMessage:
    """Persist one outbox row (flushed so the id is available); the worker delivers it later. An SMS row records
    the account in effect for its facility (`account_id`, None when nothing is configured)."""
    account = await sms_account_for(session, unit_id) if channel == "sms" else None
    return await _enqueue(
        session,
        account,
        unit_id=unit_id,
        channel=channel,
        kind=kind,
        to=to,
        text=text,
        patient_id=patient_id,
        order_id=order_id,
        document_id=document_id,
        scheduled_at=scheduled_at,
        payload=payload,
        created_by=created_by,
    )


async def enqueue_sms_if_configured(session: AsyncSession, unit_id: uuid.UUID | None, **kwargs: Any) -> OutboxMessage | None:
    """SMS only makes sense when the facility (or an ancestor) has a Xabarchi key; else the message is recorded as
    `failed` (visible in the outbox so staff can see what was not delivered and why). An invalid phone → None;
    a credential text (OTP, portal password) is never persisted without a provider → None."""
    to = norm_phone(kwargs.get("to", ""))
    if not is_valid_uz_phone(to):
        return None
    account = await sms_account_for(session, unit_id)
    configured = account is not None
    # what the outbox may show instead of a secret text (a portal password); OTP codes are masked by pattern
    masked = kwargs.pop("masked_text", None)
    if kwargs.get("kind") in SECRET_KINDS:
        # OTP codes / passwords must never be readable from the outbox: the visible text is masked and the
        # real text travels encrypted in the payload; nothing is persisted without a provider.
        if not configured:
            return None
        text = str(kwargs.get("text") or "")
        visible = str(masked) if masked is not None else mask_otp_text(text)
        kwargs = {**kwargs, "text": visible, "payload": {**(kwargs.get("payload") or {}), SECRET_TEXT_KEY: encrypt(text)}}
    msg = await _enqueue(session, account, unit_id=unit_id, channel="sms", **{**kwargs, "to": to})
    if not configured:
        msg.status = "failed"
        msg.error = SMS_NOT_CONFIGURED
        msg.next_attempt_at = None
    return msg


async def patient_home_unit(session: AsyncSession, patient: Patient) -> uuid.UUID | None:
    """The facility a patient's own messages (codes, portal access) go out from: where they were registered, else
    the facility of their latest cheque; None for a patient without either."""
    if patient.registered_unit_id:
        return patient.registered_unit_id
    return await repo.latest_order_unit(session, patient.id)


SECRET_TEXT_KEY = "secretText"
#: kinds whose text is a credential: masked in the outbox, the real text encrypted, never stored without a provider
SECRET_KINDS = frozenset({"otp", "portal_access"})


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
    unit_id: uuid.UUID | None,
    employee_id: uuid.UUID | None,
    title: str,
    body: str,
    kind: str = "info",
    link: str | None = None,
) -> Notification:
    """An in-app notice for `unit_id` and everything under it (None = everyone), optionally for one employee only."""
    n = Notification(unit_id=unit_id, employee_id=employee_id, title=title, body=body, kind=kind, link=link, read_by=[])
    session.add(n)
    await session.flush()
    return n


async def count_queued(session: AsyncSession, unit_ids: Sequence[uuid.UUID] | None) -> int:
    """Messages still waiting to go out from the given facilities (None = everywhere — the superadmin)."""
    return await repo.count_queued(session, unit_ids)


async def eskiz_delivery(session: AsyncSession, payload: Mapping[str, Any]) -> str | None:
    """An Eskiz delivery report (`callback_url`): `{request_id, message_id, phone_number, status, status_date}` —
    the outbox row that went out with that request id becomes `delivered` or `failed`; reports for states in
    between (ACCEPTED, ENROUTE …) and for unknown ids change nothing. Returns the new status, else None."""
    request_id = str(payload.get("request_id") or payload.get("id") or "").strip()
    state = str(payload.get("status") or "").strip().upper()
    if not request_id or not state:
        return None
    if state in DELIVERED_STATES:
        new_status, error = "delivered", None
    elif state in FAILED_STATES:
        new_status, error = "failed", f"eskiz:{state}"
    else:
        return None
    msg = (await session.execute(select(OutboxMessage).where(OutboxMessage.provider_message_id == request_id, OutboxMessage.channel == "sms", alive(OutboxMessage)))).scalar_one_or_none()
    if msg is None or msg.status == new_status or msg.status == "delivered":
        return None
    msg.status = new_status
    msg.error = error
    msg.updated_at = datetime.now(UTC)
    if msg.document_id:
        await mark_document_delivery(session, msg.document_id, "sms", new_status, error)
    return new_status


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


def outbox_out(msg: OutboxMessage, unit_name: str | None = None) -> OutboxMessageOut:
    dto = OutboxMessageOut.model_validate(msg)
    dto.unit_name = unit_name
    return dto


async def _outbox_dtos(session: AsyncSession, rows: Sequence[OutboxMessage]) -> list[OutboxMessageOut]:
    names = await units_repo.units_by_ids(session, {m.unit_id for m in rows})
    return [outbox_out(m, names[m.unit_id].name if m.unit_id in names else None) for m in rows]


async def list_outbox(session: AsyncSession, scope: UnitScope, q: OutboxQuery, staff: StaffPrincipal | None = None) -> Page[OutboxMessageOut]:
    """§8 `listOutbox`: everything sent from the facilities under `scope` (narrowed by `unitId`); status/kind exact,
    search on `to` digits or folded text, newest first."""
    rows, total = await repo.list_outbox(session, await narrow_to(session, scope, ids_of(q.unit_id)), q, status=q.status, kind=q.kind)
    return page_of(await _outbox_dtos(session, rows), q, total)


async def outbox_counts(session: AsyncSession, scope: UnitScope, q: OutboxQuery, staff: StaffPrincipal | None = None) -> OutboxCountsOut:
    """Status-tab counters for the messages page (same kind/search/unit filters as the list)."""
    counts = await repo.outbox_counts(session, await narrow_to(session, scope, ids_of(q.unit_id)), kind=q.kind, search=q.search)
    keys = ("scheduled", "queued", "sending", "sent", "delivered", "failed", "cancelled")
    return OutboxCountsOut(all=sum(counts.values()), **{k: counts.get(k, 0) for k in keys})


async def send(session: AsyncSession, scope: UnitScope, staff: StaffPrincipal, body: SendIn, meta: RequestMeta) -> tuple[list[OutboxMessageOut], list[str]]:
    """§8 `send`: one SMS outbox row per distinct valid recipient; broadcast (>1) needs messaging.broadcast.
    The messages go out from a facility: the path unit when it is one, else `body.unit_id` (a facility under it).

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
    facility = await units.facility_or_422(session, scope, body.unit_id)
    account = await sms_account_for(session, facility.id)
    created = [
        build_message(
            unit_id=facility.id,
            account_id=account.account_id if account else None,
            channel="sms",
            kind=body.kind,
            to=phone,
            text=body.text,
            scheduled_at=body.scheduled_at,
            created_by=staff.id,
        )
        for phone in valid
    ]
    session.add_all(created)
    await session.flush()  # one round trip for the whole broadcast
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=facility.id,
        country_id=facility.country_id,
        action="send",
        entity="outbox_message",
        entity_id=created[0].id if len(created) == 1 else None,
        after={"kind": body.kind, "recipients": len(created), "invalid": len(invalid), "scheduledAt": to_iso(body.scheduled_at), "text": body.text[:200]},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return [outbox_out(m, facility.name) for m in created], invalid


def _notification_roots(staff: StaffPrincipal) -> list[uuid.UUID] | None:
    """The scope roots whose subtree and path decide which unit-scoped notifications `staff` sees; None = all."""
    return None if staff.is_super_admin else list(staff.unit_ids)


async def list_notifications(session: AsyncSession, staff: StaffPrincipal) -> list[NotificationOut]:
    """§8 `notifications`: for everyone, for my units (or the units above them) or addressed to me, newest 50;
    `read` = me ∈ read_by."""
    rows = await repo.list_notifications(session, staff.id, _notification_roots(staff))
    return [
        NotificationOut(id=n.id, title=n.title, body=n.body, kind=n.kind, created_at=n.created_at, read=staff.id in (n.read_by or []), link=n.link)
        for n in rows
    ]


async def mark_read(session: AsyncSession, staff: StaffPrincipal, notification_id: uuid.UUID | None) -> None:
    """§8 `markRead(id?)`: one or all visible notifications become read for the caller (idempotent)."""
    await repo.mark_read(session, staff.id, _notification_roots(staff), notification_id)
