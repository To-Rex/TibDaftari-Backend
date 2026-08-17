"""Tenant business rules: companies (SMS/Telegram settings, secrets at rest), branches.

Public helpers reused by other modules: `get_company_or_404`, `company_out`, `invalidate_company_cache`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.crypto import decrypt, encrypt
from app.core.exceptions import ConflictError, ExternalServiceError, NotFoundError, ValidationError
from app.core.pagination import page_of
from app.core.permissions import DEFAULT_COMPANY_ROLES
from app.core.schemas import Page, PageQuery
from app.core.security import mask_secret
from app.core.textutil import is_valid_uz_phone, norm_phone, slugify
from app.infrastructure.db.models import Branch, Company, Role
from app.infrastructure.redis import cache
from app.modules.messaging import xabarchi
from app.modules.tenant import repository as repo
from app.modules.tenant.schemas import (
    BranchCreateIn,
    BranchOut,
    BranchUpdateIn,
    CompanyCreateIn,
    CompanyOut,
    CompanySmsIn,
    CompanySmsOut,
    CompanyTelegramOut,
    CompanyUpdateIn,
    SmsTemplates,
    SmsTestOut,
    TelegramSettingsIn,
)

log = logging.getLogger("tenant")

COMPANY_CACHE_TTL = 300
TELEGRAM_API = "https://api.telegram.org"
SMS_TEST_TEXT = "TibDaftari: SMS sozlamalari muvaffaqiyatli tekshirildi. {company}"


def _company_cache_key(company_id: uuid.UUID | str) -> str:
    return f"co:{company_id}:company"


async def invalidate_company_cache(company_id: uuid.UUID | str) -> None:
    """Drop the cached Company DTO (call after any write that changes it, incl. branch/employee counts)."""
    await cache.delete(_company_cache_key(company_id))


# ----------------------------------------------------------------------------- mapping


def company_out(company: Company, branch_count: int, employee_count: int) -> CompanyOut:
    """Company DTO — secrets are exposed only as masks; raw `settings` never leaves the backend."""
    settings_ = company.settings or {}
    return CompanyOut(
        id=str(company.id),
        name=company.name,
        legal_name=company.legal_name,
        slug=company.slug,
        logo_url=company.logo_url,
        phone=company.phone,
        email=company.email,
        address=company.address,
        locale=company.locale,  # type: ignore[arg-type]
        is_active=company.is_active,
        sms=CompanySmsOut(
            provider=company.sms_provider,  # type: ignore[arg-type]
            api_key_masked=company.sms_api_key_masked,
            default_priority=company.sms_default_priority,  # type: ignore[arg-type]
            sender_note=company.sms_sender_note,
        ),
        telegram=CompanyTelegramOut(bot_username=company.telegram_bot_username, connected=bool(company.telegram_bot_token_enc)),
        sms_templates=SmsTemplates.model_validate(settings_.get("smsTemplates") or {}),
        branch_count=branch_count,
        employee_count=employee_count,
        created_at=company.created_at,
        updated_at=company.updated_at,
    )


def branch_out(branch: Branch) -> BranchOut:
    """Branch ORM row → DTO."""
    return BranchOut(
        id=str(branch.id),
        company_id=str(branch.company_id),
        name=branch.name,
        code=branch.code,
        address=branch.address,
        phone=branch.phone,
        timezone=branch.timezone,
        is_active=branch.is_active,
        order_seq=branch.order_seq,
        created_at=branch.created_at,
        updated_at=branch.updated_at,
    )


async def get_company_or_404(session: AsyncSession, company_id: uuid.UUID | str) -> Company:
    """Alive company or 404 "Kompaniya topilmadi"."""
    try:
        cid = uuid.UUID(str(company_id))
    except ValueError as exc:
        raise NotFoundError("Kompaniya topilmadi") from exc
    company = await repo.get_company(session, cid)
    if not company:
        raise NotFoundError("Kompaniya topilmadi")
    return company


async def _company_dto(session: AsyncSession, company: Company) -> CompanyOut:
    bc, ec = await repo.company_counts(session, company.id)
    return company_out(company, bc, ec)


# ----------------------------------------------------------------------------- companies


async def list_companies(session: AsyncSession, q: PageQuery) -> Page[CompanyOut]:
    """Platform list (superadmin) with computed branch/employee counts."""
    rows, total = await repo.list_companies(session, q)
    return page_of([company_out(c, bc, ec) for c, bc, ec in rows], q, total)


async def get_company_dto(session: AsyncSession, company_id: uuid.UUID) -> CompanyOut:
    """Cached read (5 min); every write path invalidates the key."""
    key = _company_cache_key(company_id)
    hit = await cache.get_json(key)
    if hit is not None:
        return CompanyOut.model_validate(hit)
    company = await get_company_or_404(session, company_id)
    dto = await _company_dto(session, company)
    await cache.set_json(key, dto.model_dump(by_alias=True, mode="json"), COMPANY_CACHE_TTL)
    return dto


async def _ensure_slug_free(session: AsyncSession, slug: str, exclude_id: uuid.UUID | None) -> None:
    if await repo.slug_taken(session, slug, exclude_id):
        raise ConflictError("Bu slug band")


def _apply_sms(company: Company, sms: CompanySmsIn) -> None:
    """`sms` object replaced wholesale; plaintext key encrypted, only the mask is ever returned."""
    company.sms_provider = sms.provider
    company.sms_default_priority = sms.default_priority
    company.sms_sender_note = sms.sender_note or None
    if sms.provider == "none":
        company.sms_api_key_enc = None
        company.sms_api_key_masked = None
    elif sms.api_key:
        company.sms_api_key_enc = encrypt(sms.api_key)
        company.sms_api_key_masked = mask_secret(sms.api_key)
    elif not company.sms_api_key_enc:
        raise ValidationError("SMS provayder uchun API kalit kiritilmagan", code="sms_api_key_required", details={"field": "sms.apiKey"})


def _apply_sms_templates(company: Company, templates: SmsTemplates) -> None:
    current = dict(company.settings or {})
    current["smsTemplates"] = {k: v for k, v in templates.model_dump().items() if v}
    company.settings = current  # reassign → JSONB change is tracked


def _company_snapshot(company: Company) -> dict[str, Any]:
    return {
        "name": company.name,
        "legalName": company.legal_name,
        "slug": company.slug,
        "phone": company.phone,
        "email": company.email,
        "locale": company.locale,
        "isActive": company.is_active,
        "smsProvider": company.sms_provider,
        "smsApiKeyMasked": company.sms_api_key_masked,
        "telegramBotUsername": company.telegram_bot_username,
    }


async def create_company(session: AsyncSession, body: CompanyCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> CompanyOut:
    """Create a company (defaults: locale uz, active, sms none/transactional); slug unique."""
    slug = (body.slug or slugify(body.name)).strip().lower()
    await _ensure_slug_free(session, slug, None)
    company = Company(
        name=body.name,
        legal_name=body.legal_name or None,
        slug=slug,
        logo_url=body.logo_url or None,
        phone=body.phone or None,
        email=body.email or None,
        address=body.address or None,
        locale=body.locale,
        is_active=body.is_active,
        settings={},
        created_by=staff.id,
    )
    if body.sms:
        _apply_sms(company, body.sms)
    if body.sms_templates:
        _apply_sms_templates(company, body.sms_templates)
    session.add(company)
    await session.flush()
    # every company starts with the standard role set (admin is a system role) so the superadmin
    # can immediately create its first administrator
    for spec in DEFAULT_COMPANY_ROLES:
        session.add(Role(company_id=company.id, key=spec["key"], name=spec["name"], permissions=list(spec["permissions"]), is_system=bool(spec["is_system"]), created_by=staff.id))
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company.id, action="create", entity="company", entity_id=company.id, after=_company_snapshot(company), ip=meta.ip, request_id=meta.request_id)
    return company_out(company, 0, 0)


async def update_company(session: AsyncSession, company_id: uuid.UUID, body: CompanyUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> CompanyOut:
    """Partial merge of scalar fields; `sms` / `smsTemplates` objects are replaced wholesale when present."""
    company = await get_company_or_404(session, company_id)
    before = _company_snapshot(company)
    data = body.model_dump(exclude_unset=True)
    if data.get("slug"):
        slug = str(data["slug"]).strip().lower()
        if slug != company.slug:
            await _ensure_slug_free(session, slug, company.id)
        company.slug = slug
    if data.get("name"):
        company.name = data["name"]
    if data.get("locale"):
        company.locale = data["locale"]
    if data.get("is_active") is not None:
        company.is_active = data["is_active"]
    for field in ("legal_name", "logo_url", "phone", "email", "address"):
        if field in data:
            setattr(company, field, data[field] or None)
    if body.sms is not None:
        _apply_sms(company, body.sms)
    if body.sms_templates is not None:
        _apply_sms_templates(company, body.sms_templates)
    await session.flush()
    await session.refresh(company, ["updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company.id, action="update", entity="company", entity_id=company.id, before=before, after=_company_snapshot(company), ip=meta.ip, request_id=meta.request_id)
    await invalidate_company_cache(company.id)
    return await _company_dto(session, company)


# ----------------------------------------------------------------------------- SMS test / Telegram


async def send_test_sms(session: AsyncSession, company_id: uuid.UUID, to_raw: str | None, staff: StaffPrincipal, meta: RequestMeta) -> SmsTestOut:
    """Sends ONE real SMS through Xabarchi with the company's decrypted key (default recipient: company phone).

    Provider rejections (`XabarchiError`) and outages propagate as 502 with the provider message.
    """
    company = await get_company_or_404(session, company_id)
    to = norm_phone(to_raw or company.phone)
    if not is_valid_uz_phone(to):
        raise ValidationError("Telefon raqam noto‘g‘ri", code="invalid_phone")
    if company.sms_provider == "none":
        raise ValidationError("SMS provayder sozlanmagan", code="sms_not_configured")
    api_key = decrypt(company.sms_api_key_enc) or ""
    results = await xabarchi.send_sms(api_key, [to], SMS_TEST_TEXT.format(company=company.name), company.sms_default_priority)
    provider_id = results[0].provider_id if results else None
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company.id, action="sms_test", entity="company", entity_id=company.id, after={"to": to, "providerMessageId": provider_id}, ip=meta.ip, request_id=meta.request_id)
    return SmsTestOut(ok=True, provider_message_id=provider_id)


def _mask_bot_token(token: str) -> str:
    bot_id = token.split(":", 1)[0]
    return f"{bot_id}:{'•' * 8}{token[-4:]}"


async def _telegram_get_me(token: str) -> str:
    """Validates the token against Telegram (`getMe`, 5s timeout); returns the bot username."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{TELEGRAM_API}/bot{token}/getMe")
    except httpx.HTTPError as exc:
        raise ExternalServiceError("Telegram bilan aloqa yo‘q", code="telegram_unavailable") from exc
    try:
        data: dict[str, Any] = resp.json()
    except ValueError:
        data = {}
    username = str((data.get("result") or {}).get("username") or "") if data.get("ok") else ""
    if not username:
        raise ValidationError("Telegram bot tokeni noto‘g‘ri", code="telegram_invalid_token")
    return username


async def _reload_bot(company_id: uuid.UUID) -> None:
    """Best effort: ask the telegram manager to (re)start the company bot; tolerated when not implemented."""
    try:
        from app.modules.telegram.manager import bot_manager

        await bot_manager.reload_company(company_id)  # type: ignore[attr-defined]
    except (AttributeError, NotImplementedError):
        log.info("telegram bot manager has no reload_company yet — skipped for %s", company_id)
    except Exception as exc:  # pragma: no cover - never fail the settings write because of the bot
        log.warning("telegram bot reload failed for %s: %s", company_id, exc)


async def set_telegram(session: AsyncSession, company_id: uuid.UUID, body: TelegramSettingsIn, staff: StaffPrincipal, meta: RequestMeta) -> CompanyOut:
    """Connect (validated token, stored encrypted + masked + username) or disconnect (null/empty) the company bot."""
    company = await get_company_or_404(session, company_id)
    before = _company_snapshot(company)
    token = (body.bot_token or "").strip()
    if token:
        username = await _telegram_get_me(token)
        company.telegram_bot_token_enc = encrypt(token)
        company.telegram_bot_username = username
        company.telegram_bot_token_masked = _mask_bot_token(token)
        action = "telegram_connect"
    else:
        company.telegram_bot_token_enc = None
        company.telegram_bot_username = None
        company.telegram_bot_token_masked = None
        action = "telegram_disconnect"
    await session.flush()
    await session.refresh(company, ["updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company.id, action=action, entity="company", entity_id=company.id, before=before, after=_company_snapshot(company), ip=meta.ip, request_id=meta.request_id)
    await invalidate_company_cache(company.id)
    await _reload_bot(company.id)
    return await _company_dto(session, company)


# ----------------------------------------------------------------------------- branches


async def list_branches(session: AsyncSession, company_id: uuid.UUID) -> list[BranchOut]:
    """All alive branches of a company (creation order)."""
    await get_company_or_404(session, company_id)
    return [branch_out(b) for b in await repo.list_branches(session, company_id)]


async def get_branch_or_404(session: AsyncSession, branch_id: uuid.UUID, company_id: uuid.UUID | None) -> Branch:
    """Alive branch or 404 "Filial topilmadi"; `company_id=None` = platform-wide (superadmin)."""
    branch = await repo.get_branch(session, branch_id, company_id)
    if not branch:
        raise NotFoundError("Filial topilmadi")
    return branch


def _branch_snapshot(b: Branch) -> dict[str, Any]:
    return {"name": b.name, "code": b.code, "address": b.address, "phone": b.phone, "timezone": b.timezone, "isActive": b.is_active}


async def create_branch(session: AsyncSession, company_id: uuid.UUID, body: BranchCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> BranchOut:
    """Create a branch; `code` unique per company (409 "Bu filial kodi band"); orderSeq starts at 0."""
    company = await get_company_or_404(session, company_id)
    code = body.code.strip().upper()
    if await repo.branch_code_taken(session, company.id, code):
        raise ConflictError("Bu filial kodi band")
    branch = Branch(
        company_id=company.id,
        name=body.name,
        code=code,
        address=body.address or None,
        phone=body.phone or None,
        timezone=body.timezone or "Asia/Tashkent",
        is_active=body.is_active,
        order_seq=0,
        created_by=staff.id,
    )
    session.add(branch)
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company.id, action="create", entity="branch", entity_id=branch.id, after=_branch_snapshot(branch), ip=meta.ip, request_id=meta.request_id)
    await invalidate_company_cache(company.id)
    return branch_out(branch)


async def update_branch(session: AsyncSession, branch_id: uuid.UUID, body: BranchUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> BranchOut:
    """Partial merge; `orderSeq` is server-owned (never accepted from the client)."""
    branch = await get_branch_or_404(session, branch_id, None if staff.is_super_admin else staff.company_id)
    before = _branch_snapshot(branch)
    data = body.model_dump(exclude_unset=True)
    if data.get("code"):
        code = str(data["code"]).strip().upper()
        if code != branch.code and await repo.branch_code_taken(session, branch.company_id, code, branch.id):
            raise ConflictError("Bu filial kodi band")
        branch.code = code
    if data.get("name"):
        branch.name = data["name"]
    if data.get("timezone"):
        branch.timezone = data["timezone"]
    if data.get("is_active") is not None:
        branch.is_active = data["is_active"]
    for field in ("address", "phone"):
        if field in data:
            setattr(branch, field, data[field] or None)
    await session.flush()
    await session.refresh(branch, ["updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=branch.company_id, action="update", entity="branch", entity_id=branch.id, before=before, after=_branch_snapshot(branch), ip=meta.ip, request_id=meta.request_id)
    await invalidate_company_cache(branch.company_id)
    return branch_out(branch)
