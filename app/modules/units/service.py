"""Organisation-tree business rules: units (create / profile / settings), inherited SMS accounts and texts, Telegram bots.

Public helpers reused by other modules: `get_unit_or_404`, `facility_or_422`, `unit_path`, `org_name`,
`effective_settings`, `invalidate_tree_caches`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal, invalidate_principal_cache
from app.core.audit import audit
from app.core.crypto import decrypt, encrypt
from app.core.exceptions import ConflictError, ExternalServiceError, ForbiddenError, NotFoundError, ValidationError
from app.core.scope import UnitScope, invalidate_scope_cache, load_scope
from app.core.security import mask_secret
from app.core.textutil import is_valid_uz_phone, norm_phone
from app.core.timeutil import utcnow
from app.infrastructure.db.models import CHILD_LEVEL, MessagingAccount, OrgUnit, SmsTemplate
from app.infrastructure.redis import cache
from app.modules.messaging import eskiz, sms
from app.modules.messaging.eskiz import EskizAuthError
from app.modules.messaging.providers import (
    SMS_PROVIDERS,
    SmsAccount,
    SmsProviderError,
    SmsTransientError,
    account_to_sms,
)
from app.modules.units import repository as repo
from app.modules.units.schemas import (
    EskizInfoOut,
    EskizPriceOut,
    EskizTemplateIn,
    EskizTemplateOut,
    OrgUnitOut,
    SmsCheckOut,
    SmsTemplates,
    SmsTestOut,
    TelegramSettingsIn,
    UnitCounts,
    UnitCreateIn,
    UnitSettingsOut,
    UnitSmsIn,
    UnitSmsOut,
    UnitSmsTemplatesIn,
    UnitSmsTemplatesOut,
    UnitTelegramOut,
    UnitTreeQuery,
    UnitUpdateIn,
    document_free_view_days,
)

log = logging.getLogger("units")

UNIT_CACHE_TTL = 300
TELEGRAM_API = "https://api.telegram.org"
SMS_TEST_TEXT = "TibDaftari: SMS sozlamalari muvaffaqiyatli tekshirildi. {org}"
FACILITY_REQUIRED = "Bu amal faqat muassasa darajasida bajariladi"
PLATFORM_MANAGE = "platform.unit.manage"


def _dto_key(unit_id: uuid.UUID | str) -> str:
    return f"unit:{unit_id}:dto"


async def invalidate_tree_caches(unit_id: uuid.UUID | str | None = None) -> None:
    """After any org-tree write: scopes, cached unit cards and every principal (their allowed set may have changed)."""
    await invalidate_scope_cache()
    if unit_id is not None:
        await cache.delete(_dto_key(unit_id))
    # inherited catalogue / templates / roles lists are keyed per requesting unit — membership may have changed
    for prefix in ("catalog:", "tpl:", "roles:"):
        await cache.delete_prefix(prefix)
    await invalidate_principal_cache()


# ----------------------------------------------------------------------------- lookups


def _uuid_or_422(value: Any, label: str) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ValidationError(f"{label} topilmadi") from exc


async def get_unit_or_404(session: AsyncSession, unit_id: uuid.UUID | str) -> OrgUnit:
    """Alive unit or 404 "Birlik topilmadi"."""
    try:
        uid = uuid.UUID(str(unit_id))
    except ValueError as exc:
        raise NotFoundError("Birlik topilmadi") from exc
    unit = await repo.get_unit(session, uid)
    if not unit:
        raise NotFoundError("Birlik topilmadi")
    return unit


async def scope_or_404(session: AsyncSession, unit_id: uuid.UUID | str) -> UnitScope:
    scope = await load_scope(session, unit_id)
    if scope is None:
        raise NotFoundError("Birlik topilmadi")
    return scope


async def facility_or_422(session: AsyncSession, scope: UnitScope, requested: str | uuid.UUID | None = None) -> OrgUnit:
    """The facility an operation runs in: the path unit when it is a facility, else `requested` (a facility under
    the path unit). 422 `facility_required` otherwise."""
    if requested:
        uid = _uuid_or_422(requested, "Muassasa")
        if uid is None or not scope.contains(uid):
            raise NotFoundError("Muassasa topilmadi")
        unit = await repo.get_unit(session, uid)
        if unit is None:
            raise NotFoundError("Muassasa topilmadi")
        if unit.level != "facility":
            raise ValidationError(FACILITY_REQUIRED, code="facility_required")
        return unit
    if scope.is_facility:
        return scope.unit
    raise ValidationError(FACILITY_REQUIRED, code="facility_required")


async def unit_path(session: AsyncSession, unit_id: uuid.UUID) -> str:
    return await repo.unit_path(session, unit_id)


async def org_name(session: AsyncSession, unit_id: uuid.UUID | None) -> str:
    """The organisation a unit belongs to, for message texts: the nearest ancestor above facility level
    (its short name when it has one), else the unit's own name."""
    if unit_id is None:
        return ""
    scope = await load_scope(session, unit_id)
    if scope is None:
        unit = await repo.get_unit_any(session, unit_id)
        return (unit.short_name or unit.name) if unit else ""
    units = await repo.units_by_ids(session, scope.ancestor_ids)
    for uid in reversed(scope.ancestor_ids):
        u = units.get(uid)
        if u is not None and u.level != "facility":
            return u.short_name or u.name
    return scope.unit.short_name or scope.unit.name


async def effective_settings(session: AsyncSession, unit_id: uuid.UUID) -> dict[str, Any]:
    return await repo.effective_settings(session, unit_id)


def visible_ids_for(staff: StaffPrincipal) -> list[uuid.UUID] | None:
    """Units a staff member may read in the tree: their scope; None = everything (superadmin)."""
    if staff.is_super_admin or staff.allowed_unit_ids is None:
        return None
    return list(staff.allowed_unit_ids)


async def require_visible(session: AsyncSession, staff: StaffPrincipal, unit: OrgUnit) -> None:
    """A unit may be read when it lies in the caller's scope OR on the path above it (context for breadcrumbs)."""
    if staff.can_see(unit.id):
        return
    context = await repo.ancestors_of(session, staff.unit_ids)
    if unit.id not in context:
        raise ForbiddenError("Ruxsat yo‘q")


# ----------------------------------------------------------------------------- mapping


def _settings_out(effective: dict[str, Any], own: dict[str, Any]) -> UnitSettingsOut:
    locale = effective.get("locale")
    return UnitSettingsOut(document_free_view_days=document_free_view_days(effective), locale=locale if locale in ("uz", "ru", "en") else None, own=dict(own or {}))


def sms_out(unit: OrgUnit, ancestor_ids: list[uuid.UUID], accounts: dict[uuid.UUID, MessagingAccount], names: dict[uuid.UUID, str]) -> UnitSmsOut:
    own = accounts.get(unit.id)
    effective = repo.nearest_account(ancestor_ids, accounts)
    if own is not None and own.is_active:
        mode = "off" if own.provider == "none" else "own"
    else:
        mode = "inherited"
    configured = effective is not None and effective.provider in SMS_PROVIDERS and bool(effective.api_key_enc)
    # the credentials shown: the unit's own live account, else the account in effect above it
    shown = own if own is not None and own.provider in SMS_PROVIDERS else (effective if configured else None)
    return UnitSmsOut(
        mode=mode,  # type: ignore[arg-type]
        provider=effective.provider if configured and effective is not None else "none",  # type: ignore[arg-type]
        api_key_masked=shown.api_key_masked if shown is not None else None,
        login=shown.login if shown is not None else None,
        sender=shown.sender if shown is not None else None,
        default_priority=(effective.default_priority if effective is not None else "transactional"),  # type: ignore[arg-type]
        sender_note=own.sender_note if own is not None else (effective.sender_note if effective is not None else None),
        effective_unit_id=str(effective.unit_id) if configured and effective is not None else None,
        effective_unit_name=names.get(effective.unit_id) if configured and effective is not None else None,
    )


def telegram_out(unit: OrgUnit, ancestor_ids: list[uuid.UUID], accounts: dict[uuid.UUID, MessagingAccount]) -> UnitTelegramOut:
    own = accounts.get(unit.id)
    effective = repo.nearest_account(ancestor_ids, accounts)
    return UnitTelegramOut(
        bot_username=effective.bot_username if effective is not None else None,
        connected=effective is not None,
        own=own is not None and own.is_active,
        effective_unit_id=str(effective.unit_id) if effective is not None else None,
    )


def unit_out(
    unit: OrgUnit,
    *,
    names: dict[str, str],
    depth: int,
    path: str,
    counts: dict[str, int] | None,
    effective: dict[str, Any],
    sms: UnitSmsOut | None = None,
    telegram: UnitTelegramOut | None = None,
) -> OrgUnitOut:
    return OrgUnitOut(
        id=str(unit.id),
        level=unit.level,  # type: ignore[arg-type]
        kind=unit.kind,  # type: ignore[arg-type]
        parent_id=str(unit.parent_id) if unit.parent_id else None,
        country_id=str(unit.country_id),
        region_id=str(unit.region_id) if unit.region_id else None,
        district_id=str(unit.district_id) if unit.district_id else None,
        country_name=names.get(str(unit.country_id)),
        region_name=names.get(str(unit.region_id)) if unit.region_id else None,
        district_name=names.get(str(unit.district_id)) if unit.district_id else None,
        name=unit.name,
        short_name=unit.short_name,
        code=unit.code,
        legal_name=unit.legal_name,
        phone=unit.phone,
        email=unit.email,
        address=unit.address,
        timezone=unit.timezone or "Asia/Tashkent",
        logo_url=f"/api/v1/files/{unit.logo_file_id}" if unit.logo_file_id else None,
        is_active=unit.is_active,
        sort_order=unit.sort_order,
        depth=depth,
        path=path,
        settings=_settings_out(effective, unit.settings or {}),
        sms=sms,
        telegram=telegram,
        counts=UnitCounts(**(counts or {})),
        created_at=unit.created_at,
        updated_at=unit.updated_at,
    )


async def unit_dto(session: AsyncSession, unit: OrgUnit) -> OrgUnitOut:
    """The full unit card (geo names, counts, effective settings, SMS / Telegram status). Cached 5 min."""
    key = _dto_key(unit.id)
    hit = await cache.get_json(key)
    if hit is not None:
        return OrgUnitOut.model_validate(hit)
    scope = await scope_or_404(session, unit.id)
    names = await repo.geo_names(session, [unit])
    depth = (await repo.depths(session, [unit.id])).get(unit.id, 0)
    path = await repo.unit_path(session, unit.id)
    counts = (await repo.counts(session, [unit.id])).get(unit.id)
    effective = await repo.effective_settings(session, unit.id)
    sms_accounts = await repo.accounts_on_path(session, scope.ancestor_ids, "sms")
    tg_accounts = await repo.accounts_on_path(session, scope.ancestor_ids, "telegram")
    unit_names = {u.id: u.name for u in (await repo.units_by_ids(session, scope.ancestor_ids)).values()}
    dto = unit_out(
        unit,
        names=names,
        depth=depth,
        path=path,
        counts=counts,
        effective=effective,
        sms=sms_out(unit, scope.ancestor_ids, sms_accounts, unit_names),
        telegram=telegram_out(unit, scope.ancestor_ids, tg_accounts),
    )
    await cache.set_json(key, dto.model_dump(by_alias=True, mode="json"), UNIT_CACHE_TTL)
    return dto


async def get_unit_dto(session: AsyncSession, unit_id: uuid.UUID, staff: StaffPrincipal) -> OrgUnitOut:
    unit = await get_unit_or_404(session, unit_id)
    await require_visible(session, staff, unit)
    return await unit_dto(session, unit)


async def list_tree(session: AsyncSession, staff: StaffPrincipal, q: UnitTreeQuery) -> list[OrgUnitOut]:
    """Every unit the caller may see, flat, with parentId / depth / path so the UI builds the tree.
    Scoped staff get their subtree(s) plus the path above (read-only context). Light cards: no SMS / Telegram."""
    visible = visible_ids_for(staff)
    if visible is not None:
        visible = list(dict.fromkeys([*visible, *(await repo.ancestors_of(session, staff.unit_ids))]))
    country_id = _uuid_or_422(q.country_id, "Davlat")
    root_id = _uuid_or_422(q.root_id, "Birlik")
    if root_id is not None:
        subtree = set(await repo.subtree_ids(session, root_id))
        visible = [u for u in (visible if visible is not None else subtree) if u in subtree]
    units = await repo.list_units(session, visible, country_id=country_id, include_inactive=q.include_inactive)
    if not units:
        return []
    ids = [u.id for u in units]
    names = await repo.geo_names(session, units)
    depth = await repo.depths(session, ids)
    path = await repo.paths(session, ids)
    counts = await repo.counts(session, ids)
    out: list[OrgUnitOut] = []
    for u in units:
        out.append(unit_out(u, names=names, depth=depth.get(u.id, 0), path=path.get(u.id, u.name), counts=counts.get(u.id), effective=u.settings or {}))
    # the effective settings need the whole path: compute once per unit in Python from the already loaded rows
    by_id = {u.id: u for u in units}
    for dto in out:
        merged: dict[str, Any] = {}
        chain: list[OrgUnit] = []
        cur = by_id.get(uuid.UUID(dto.id))
        while cur is not None:
            chain.append(cur)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        for node in reversed(chain):
            merged.update(node.settings or {})
        dto.settings = _settings_out(merged, by_id[uuid.UUID(dto.id)].settings or {})
    return sorted(out, key=lambda d: (d.depth, d.sort_order, d.name))


# ----------------------------------------------------------------------------- create / update


def _snapshot(u: OrgUnit) -> dict[str, Any]:
    return {
        "level": u.level,
        "kind": u.kind,
        "parentId": str(u.parent_id) if u.parent_id else None,
        "name": u.name,
        "shortName": u.short_name,
        "code": u.code,
        "legalName": u.legal_name,
        "phone": u.phone,
        "email": u.email,
        "address": u.address,
        "timezone": u.timezone,
        "isActive": u.is_active,
        "sortOrder": u.sort_order,
        "countryId": str(u.country_id),
        "regionId": str(u.region_id) if u.region_id else None,
        "districtId": str(u.district_id) if u.district_id else None,
        "settings": u.settings,
    }


async def create_unit(session: AsyncSession, body: UnitCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> OrgUnitOut:
    """A child of `parentId` (level = the parent's next level) or, without a parent, a country node (platform only).
    Geo nesting is validated here and again by the DB trigger; facility codes are unique (409 "Bu kod band")."""
    parent: OrgUnit | None = None
    if body.parent_id:
        parent = await get_unit_or_404(session, body.parent_id)
        staff.require_unit(parent.id)
        if not staff.has("admin.unit.manage", PLATFORM_MANAGE):
            raise ForbiddenError("Ruxsat yo‘q")
        level = CHILD_LEVEL[parent.level]
        if level is None:
            raise ValidationError("Muassasa ostida birlik yaratib bo‘lmaydi", code="leaf")
    else:
        staff.require(PLATFORM_MANAGE)
        level = "country"

    country_id = parent.country_id if parent else _uuid_or_422(body.country_id, "Davlat")
    if country_id is None or await repo.get_country(session, country_id) is None:
        raise ValidationError("Davlat topilmadi")
    region_id = parent.region_id if parent else None
    district_id = parent.district_id if parent else None
    if level == "region":
        region_id = _uuid_or_422(body.region_id, "Viloyat")
        region = await repo.get_region(session, region_id) if region_id else None
        if region is None or region.country_id != country_id:
            raise ValidationError("Viloyat tanlangan davlatga tegishli emas" if region else "Viloyat topilmadi")
        if await repo.geo_node_exists(session, "region", region.id):
            raise ConflictError("Bu viloyat uchun birlik allaqachon mavjud", code="node_exists")
    elif level == "district":
        district_id = _uuid_or_422(body.district_id, "Tuman")
        district = await repo.get_district(session, district_id) if district_id else None
        if district is None or district.region_id != region_id:
            raise ValidationError("Tuman tanlangan viloyatga tegishli emas" if district else "Tuman topilmadi")
        if await repo.geo_node_exists(session, "district", district.id):
            raise ConflictError("Bu tuman uchun birlik allaqachon mavjud", code="node_exists")
    kind = body.kind if level == "facility" else None
    if level == "facility" and not kind:
        raise ValidationError("Muassasa turi tanlanmagan", code="kind_required")
    code = (body.code or "").strip().upper() or None
    if level == "facility" and not code:
        raise ValidationError("Muassasa kodi kiritilmagan", code="code_required")
    if code and await repo.code_taken(session, code):
        raise ConflictError("Bu kod band")
    unit = OrgUnit(
        level=level,
        parent_id=parent.id if parent else None,
        country_id=country_id,
        region_id=region_id,
        district_id=district_id,
        kind=kind,
        name=body.name,
        short_name=body.short_name or None,
        code=code,
        legal_name=body.legal_name or None,
        phone=body.phone or None,
        email=body.email or None,
        address=body.address or None,
        timezone=body.timezone or None,
        is_active=body.is_active,
        sort_order=body.sort_order,
        settings={},
        created_by=staff.id,
    )
    session.add(unit)
    await session.flush()
    await session.refresh(unit)
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=unit.id, country_id=unit.country_id, action="create", entity="unit", entity_id=unit.id, after=_snapshot(unit), ip=meta.ip, request_id=meta.request_id)
    if level == "facility" and parent is not None:
        # an organisation gets the standard cheque together with its first facility (a draft bound to it)
        from app.modules.templates import service as templates_svc

        await templates_svc.ensure_default_receipt(session, parent.id, [unit.id], None, staff.id)
    await invalidate_tree_caches(unit.id)
    if parent is not None:
        await cache.delete(_dto_key(parent.id))
    return await unit_dto(session, unit)


async def update_unit(session: AsyncSession, unit_id: uuid.UUID, body: UnitUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> OrgUnitOut:
    """Partial merge of the profile; `documentFreeViewDays` / `locale` go into the unit's own `settings`."""
    unit = await get_unit_or_404(session, unit_id)
    staff.require_unit(unit.id)
    before = _snapshot(unit)
    data = body.model_dump(exclude_unset=True)
    if data.get("code") is not None:
        code = str(data["code"]).strip().upper()
        if code != (unit.code or "") and await repo.code_taken(session, code, unit.id):
            raise ConflictError("Bu kod band")
        unit.code = code or None
    if data.get("name"):
        unit.name = data["name"]
    if data.get("kind") and unit.level == "facility":
        unit.kind = data["kind"]
    if data.get("timezone"):
        unit.timezone = data["timezone"]
    if data.get("is_active") is not None:
        unit.is_active = data["is_active"]
    if data.get("sort_order") is not None:
        unit.sort_order = int(data["sort_order"])
    for field in ("short_name", "legal_name", "phone", "email", "address"):
        if field in data:
            setattr(unit, field, data[field] or None)
    if "logo_url" in data:
        raw = (data["logo_url"] or "").rstrip("/")
        unit.logo_file_id = _uuid_or_422(raw.rsplit("/", 1)[-1], "Logotip") if raw else None
    own = dict(unit.settings or {})
    if body.document_free_view_days is not None:
        # read live by every public-link request: shortening it (or 0) closes the code-free links at once
        own["documentFreeViewDays"] = body.document_free_view_days
    if "locale" in data:
        if data["locale"]:
            own["locale"] = data["locale"]
        else:
            own.pop("locale", None)
    unit.settings = own  # reassign → JSONB change is tracked
    await session.flush()
    await session.refresh(unit, ["updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=unit.id, country_id=unit.country_id, action="update", entity="unit", entity_id=unit.id, before=before, after=_snapshot(unit), ip=meta.ip, request_id=meta.request_id)
    await invalidate_tree_caches(unit.id)
    return await unit_dto(session, unit)


# ----------------------------------------------------------------------------- SMS account


async def get_sms(session: AsyncSession, unit_id: uuid.UUID, staff: StaffPrincipal) -> UnitSmsOut:
    unit = await get_unit_or_404(session, unit_id)
    staff.require_unit(unit.id)
    scope = await scope_or_404(session, unit.id)
    accounts = await repo.accounts_on_path(session, scope.ancestor_ids, "sms")
    names = {u.id: u.name for u in (await repo.units_by_ids(session, scope.ancestor_ids)).values()}
    return sms_out(unit, scope.ancestor_ids, accounts, names)


def _retire_account(acc: MessagingAccount, staff: StaffPrincipal) -> None:
    """Drop a unit's own messaging account the way every business row goes: soft-deleted. Outbox rows already sent
    with it keep pointing at it (`outbox_messages.account_id`); the resolvers only look at alive, active rows."""
    acc.is_active = False
    acc.deleted_at = utcnow()
    acc.deleted_by = staff.id


def _sms_snapshot(acc: MessagingAccount | None) -> dict[str, Any]:
    if acc is None:
        return {"mode": "inherited"}
    return {"mode": "off" if acc.provider == "none" else "own", "provider": acc.provider, "login": acc.login, "sender": acc.sender, "apiKeyMasked": acc.api_key_masked, "defaultPriority": acc.default_priority, "senderNote": acc.sender_note}


PASSWORD_MASK = "••••••••"


def _own_credentials(own: MessagingAccount, body: UnitSmsIn) -> None:
    """The own account's provider and credentials from the request: Xabarchi — a key (new, or the saved one);
    Eskiz — the cabinet e-mail, the password (new, or the saved one) and the sender name. Switching providers
    always needs the new provider's secret."""
    provider = body.provider or (own.provider if own.provider in SMS_PROVIDERS else "xabarchi")
    same = own.provider == provider and bool(own.api_key_enc)
    if provider == "eskiz":
        login = (body.login or (own.login if same else None) or "").strip()
        if not login:
            raise ValidationError("Eskiz kabinet e-mail (login) kiritilmagan", code="sms_login_required", details={"field": "login"})
        password = (body.password or "").strip()
        if password:
            own.api_key_enc = encrypt(password)
            own.api_key_masked = PASSWORD_MASK
        elif not same:
            raise ValidationError("Eskiz paroli kiritilmagan", code="sms_api_key_required", details={"field": "password"})
        own.login = login
        own.sender = ((body.sender or "").strip() or (own.sender if same else None) or eskiz.DEFAULT_SENDER)[:40]
    else:
        key = (body.api_key or "").strip()
        if key:
            own.api_key_enc = encrypt(key)
            own.api_key_masked = mask_secret(key)
        elif not same:
            raise ValidationError("Xabarchi API kaliti kiritilmagan", code="sms_api_key_required", details={"field": "apiKey"})
        own.login = None
        own.sender = None
    own.provider = provider


async def _verify_eskiz(own: MessagingAccount) -> None:
    """A fresh Eskiz login with the saved credentials: wrong ones → 422 `sms_auth_error` (nothing is saved);
    Eskiz unreachable → the account is saved anyway, the "check connection" button tells more."""
    try:
        await eskiz.check_account(own.id, own.login or "", decrypt(own.api_key_enc or "") or "")
    except SmsProviderError as exc:
        raise ValidationError(exc.message, code="sms_auth_error", details={"field": "password"}) from exc
    except SmsTransientError as exc:
        log.warning("eskiz: account of unit %s saved without verification: %s", own.unit_id, exc.message)


async def set_sms(session: AsyncSession, unit_id: uuid.UUID, body: UnitSmsIn, staff: StaffPrincipal, meta: RequestMeta) -> UnitSmsOut:
    """Save the unit's own SMS account — units above and beside it keep theirs; units below inherit this one."""
    unit = await get_unit_or_404(session, unit_id)
    staff.require_unit(unit.id)
    own = await repo.own_account(session, unit.id, "sms")
    before = _sms_snapshot(own)
    if body.mode == "inherited":
        if own is not None:
            _retire_account(own, staff)
    else:
        if own is None:
            own = MessagingAccount(unit_id=unit.id, channel="sms", provider="none", created_by=staff.id)
            session.add(own)
        own.is_active = True
        if body.mode == "off":
            own.provider = "none"
            own.api_key_enc = None
            own.api_key_masked = None
            own.login = None
            own.sender = None
            own.sender_note = body.sender_note or None
        else:
            _own_credentials(own, body)
            own.default_priority = body.default_priority or own.default_priority or "transactional"
            own.sender_note = body.sender_note or None
    await session.flush()
    if body.mode == "own" and own is not None and own.provider == "eskiz":
        await _verify_eskiz(own)
    after = _sms_snapshot(None if body.mode == "inherited" else own)
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=unit.id, country_id=unit.country_id, action="sms_account", entity="unit", entity_id=unit.id, before=before, after=after, ip=meta.ip, request_id=meta.request_id)
    await invalidate_tree_caches(unit.id)
    return await get_sms(session, unit.id, staff)


async def send_test_sms(session: AsyncSession, unit_id: uuid.UUID, to_raw: str | None, staff: StaffPrincipal, meta: RequestMeta) -> SmsTestOut:
    """ONE real SMS through the key this unit's messages use (default recipient: the unit's phone, else the nearest
    ancestor's). Provider errors propagate as 502 with the provider message."""
    unit = await get_unit_or_404(session, unit_id)
    staff.require_unit(unit.id)
    scope = await scope_or_404(session, unit.id)
    account = account_to_sms(await repo.resolve_account(session, unit.id, "sms"))
    if account is None:
        raise ValidationError("SMS provayder sozlanmagan", code="sms_not_configured")
    phone = unit.phone
    if not phone:
        chain = await repo.units_by_ids(session, scope.ancestor_ids)
        for uid in reversed(scope.ancestor_ids):
            if chain.get(uid) is not None and chain[uid].phone:
                phone = chain[uid].phone
                break
    to = norm_phone(to_raw or phone)
    if not is_valid_uz_phone(to):
        raise ValidationError("Telefon raqam noto‘g‘ri", code="invalid_phone")
    result = await sms.send(account, to, sms.test_text(account, SMS_TEST_TEXT.format(org=unit.short_name or unit.name)))
    balance = await eskiz.account_balance(account.account_id or account.unit_id, account.login or "", decrypt(account.key_enc) or "") if account.provider == "eskiz" else None
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=unit.id, country_id=unit.country_id, action="sms_test", entity="unit", entity_id=unit.id, after={"to": to, "provider": account.provider, "providerMessageId": result.provider_id, "accountUnitId": str(account.unit_id)}, ip=meta.ip, request_id=meta.request_id)
    return SmsTestOut(ok=True, provider=account.provider, provider_message_id=result.provider_id, to=to, balance=balance)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- Eskiz panel (balance, nicks, prices, templates)

ESKIZ_INFO_TTL = 60
#: below this balance (so‘m) the card warns
LOW_BALANCE = 30_000


def _eskiz_info_key(account_id: uuid.UUID | None) -> str:
    return f"eskiz:info:{account_id}"


async def _eskiz_account(session: AsyncSession, unit: OrgUnit) -> tuple[SmsAccount, MessagingAccount] | None:
    """The Eskiz account in effect for the unit (own or inherited), or None when SMS goes another way."""
    row = await repo.resolve_account(session, unit.id, "sms")
    acc = account_to_sms(row)
    return (acc, row) if acc is not None and row is not None and acc.provider == "eskiz" else None


async def _soft[T](call: Any, default: T) -> T:
    """A piece of the panel Eskiz refuses (an endpoint off for the account) is left empty; a dead token still
    bubbles up so the caller logs in again."""
    try:
        return await call
    except EskizAuthError:
        raise
    except SmsProviderError as exc:
        log.info("eskiz: part of the panel unavailable: %s", exc.message)
        return default


async def _fetch_eskiz(acc: SmsAccount) -> dict[str, Any]:
    secret = decrypt(acc.key_enc) or ""

    async def gather(tok: str) -> dict[str, Any]:
        user = await _soft(eskiz.me(tok), {})
        balance = await _soft(eskiz.balance(tok), None)
        if balance is None and user.get("balance") not in (None, ""):
            try:
                balance = int(float(user["balance"]))
            except (TypeError, ValueError):
                balance = None
        return {
            "user": user,
            "balance": balance,
            "nicks": await _soft(eskiz.nicks(tok), []),
            "prices": await _soft(eskiz.prices(tok), []),
            "templates": await _soft(eskiz.templates(tok), []),
        }

    return await eskiz.call_with_account(acc.account_id or acc.unit_id, acc.login or "", secret, gather)


async def eskiz_info(session: AsyncSession, unit_id: uuid.UUID, staff: StaffPrincipal, *, refresh: bool = False) -> EskizInfoOut:
    """The Eskiz panel of a unit: `configured=false` when SMS goes another way; otherwise what Eskiz says about the
    account (cached per account for a minute; `refresh` asks again). An unreachable Eskiz gives `error`."""
    unit = await get_unit_or_404(session, unit_id)
    staff.require_unit(unit.id)
    pair = await _eskiz_account(session, unit)
    if pair is None:
        return EskizInfoOut(configured=False)
    acc, row = pair
    names = await repo.units_by_ids(session, [row.unit_id])
    base = {"configured": True, "account_unit_id": str(row.unit_id), "account_unit_name": names[row.unit_id].name if row.unit_id in names else None, "login": acc.login, "sender": acc.sender}
    key = _eskiz_info_key(acc.account_id)
    if not refresh:
        cached = await cache.get_json(key)
        if isinstance(cached, dict):
            return EskizInfoOut.model_validate({**cached, **base})
    try:
        data = await _fetch_eskiz(acc)
    except (SmsProviderError, SmsTransientError) as exc:
        return EskizInfoOut(**base, error=exc.message)
    user = data["user"] if isinstance(data.get("user"), dict) else {}
    out = EskizInfoOut(
        **base,
        balance=data["balance"],
        low_balance=data["balance"] is not None and data["balance"] < LOW_BALANCE,
        nicks=list(data["nicks"]),
        prices=[EskizPriceOut(operator=str(p["operator"]), price=float(p["price"]), country_code=p.get("countryCode")) for p in data["prices"]],
        templates=[EskizTemplateOut(id=str(t["id"]), text=str(t["text"]), status=str(t["status"])) for t in data["templates"]],
        user_name=str(user["name"]) if user.get("name") else None,
        user_status=str(user["status"]) if user.get("status") else None,
        fetched_at=utcnow(),
    )
    await cache.set_json(key, out.model_dump(mode="json"), ESKIZ_INFO_TTL)
    return out


async def eskiz_submit_template(session: AsyncSession, unit_id: uuid.UUID, body: EskizTemplateIn, staff: StaffPrincipal, meta: RequestMeta) -> EskizInfoOut:
    """Hand a text to Eskiz for moderation with the account in effect; the panel comes back refreshed."""
    unit = await get_unit_or_404(session, unit_id)
    staff.require_unit(unit.id)
    pair = await _eskiz_account(session, unit)
    if pair is None:
        raise ValidationError("Bu birlik uchun Eskiz hisobi sozlanmagan", code="sms_not_configured")
    acc, _ = pair
    text = body.text.strip()
    try:
        answer = await eskiz.call_with_account(acc.account_id or acc.unit_id, acc.login or "", decrypt(acc.key_enc) or "", lambda tok: eskiz.create_template(tok, text))
    except SmsProviderError as exc:
        raise ValidationError(exc.message, code="sms_rejected", details={"field": "text"}) from exc
    except SmsTransientError as exc:
        raise ExternalServiceError(exc.message, code="sms_provider_unavailable") from exc
    await cache.delete(_eskiz_info_key(acc.account_id))
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=unit.id, country_id=unit.country_id, action="sms_template_submit", entity="unit", entity_id=unit.id, after={"text": text[:300], "answer": {k: v for k, v in answer.items() if k in ("id", "status", "template")}}, ip=meta.ip, request_id=meta.request_id)
    return await eskiz_info(session, unit.id, staff, refresh=True)


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


async def check_sms_text(session: AsyncSession, unit_id: uuid.UUID, text: str, staff: StaffPrincipal) -> SmsCheckOut:
    """Parts the operators bill for `text` (GSM-7 / UCS-2) and, with an Eskiz account in effect, the price range over
    the operators (Eskiz's own check wins when it answers) and the moderation state of a matching template."""
    unit = await get_unit_or_404(session, unit_id)
    staff.require_unit(unit.id)
    encoding, length, parts = eskiz.segments(text)
    row = await repo.resolve_account(session, unit.id, "sms")
    acc = account_to_sms(row)
    out = SmsCheckOut(provider=acc.provider if acc else "none", encoding=encoding, length=length, parts=parts)  # type: ignore[arg-type]
    if acc is None or acc.provider != "eskiz":
        return out
    info = await cache.get_json(_eskiz_info_key(acc.account_id))
    if isinstance(info, dict):
        prices = [float(p["price"]) for p in info.get("prices") or [] if not p.get("country_code") or str(p["country_code"]).upper() == "UZ"]
        if prices and parts:
            out.price_from, out.price_to = parts * min(prices), parts * max(prices)
        for tpl in info.get("templates") or []:
            if _same_text(str(tpl.get("text") or ""), text):
                out.template_status = str(tpl.get("status") or "moderation")
                break
    if text.strip():
        try:
            checked = await eskiz.call_with_account(acc.account_id or acc.unit_id, acc.login or "", decrypt(acc.key_enc) or "", lambda tok: eskiz.check_message(tok, text))
        except (SmsProviderError, SmsTransientError):
            checked = None
        if checked:
            if checked.get("parts"):
                out.parts = int(checked["parts"])
            if checked.get("price") is not None:
                out.price_from = out.price_to = float(checked["price"])
    return out


# ----------------------------------------------------------------------------- SMS texts


async def _templates_out(session: AsyncSession, unit: OrgUnit, applied: int = 0) -> UnitSmsTemplatesOut:
    scope = await scope_or_404(session, unit.id)
    texts = await repo.sms_texts_on_path(session, scope.ancestor_ids)
    own = texts.get(unit.id, {})
    merged = repo.merge_texts(scope.ancestor_ids, texts)
    return UnitSmsTemplatesOut(unit_id=str(unit.id), templates=SmsTemplates.model_validate(merged), own=SmsTemplates.model_validate(own), inherited=not own, applied=applied)


async def get_sms_templates(session: AsyncSession, unit_id: uuid.UUID, staff: StaffPrincipal) -> UnitSmsTemplatesOut:
    unit = await get_unit_or_404(session, unit_id)
    staff.require_unit(unit.id)
    return await _templates_out(session, unit)


async def _write_texts(session: AsyncSession, unit_id: uuid.UUID, texts: dict[str, str], staff: StaffPrincipal) -> dict[str, str | None]:
    """Replace a unit's own texts with `texts` (empty value = drop the override). Returns the previous texts."""
    rows = {t.key: t for t in await repo.own_sms_texts(session, unit_id)}
    before = {k: t.body for k, t in rows.items()}
    for key, body in texts.items():
        row = rows.get(key)
        if body:
            if row is None:
                session.add(SmsTemplate(unit_id=unit_id, key=key, lang="uz", body=body, is_active=True, created_by=staff.id))
            else:
                row.body = body
                row.is_active = True
        elif row is not None:
            await session.delete(row)
    for key, row in rows.items():
        if key not in texts:
            await session.delete(row)
    return before


async def set_sms_templates(session: AsyncSession, unit_id: uuid.UUID, body: UnitSmsTemplatesIn, staff: StaffPrincipal, meta: RequestMeta) -> UnitSmsTemplatesOut:
    """Save the unit's own SMS texts (units below inherit them). `applyToAll`: the same texts on every unit under
    it as well — they stop inheriting."""
    unit = await get_unit_or_404(session, unit_id)
    staff.require_unit(unit.id)
    texts = {k: v for k, v in body.templates.model_dump().items() if v}
    targets = [unit.id]
    if body.apply_to_all:
        targets = await repo.subtree_ids(session, unit.id)
    for uid in targets:
        before = await _write_texts(session, uid, texts, staff)
        await audit(session, actor_type="staff", actor_id=staff.id, unit_id=uid, country_id=unit.country_id, action="sms_templates", entity="unit", entity_id=uid, before={"smsTemplates": before}, after={"smsTemplates": texts, "applyToAll": body.apply_to_all}, ip=meta.ip, request_id=meta.request_id)
    await session.flush()
    await invalidate_tree_caches(unit.id)
    return await _templates_out(session, unit, applied=len(targets))


# ----------------------------------------------------------------------------- Telegram


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


async def _reload_bot(account_id: uuid.UUID | None) -> None:
    """Best effort: ask the telegram manager to (re)start the bot of this account."""
    if account_id is None:
        return
    try:
        from app.modules.telegram.manager import bot_manager

        await bot_manager.reload_account(account_id)  # type: ignore[attr-defined]
    except (AttributeError, NotImplementedError):
        log.info("telegram bot manager has no reload_account yet — skipped for %s", account_id)
    except Exception as exc:  # pragma: no cover - never fail the settings write because of the bot
        log.warning("telegram bot reload failed for %s: %s", account_id, exc)


async def set_telegram(session: AsyncSession, unit_id: uuid.UUID, body: TelegramSettingsIn, staff: StaffPrincipal, meta: RequestMeta) -> OrgUnitOut:
    """Connect (validated token, stored encrypted + masked + username) or disconnect (null/empty) the unit's own bot."""
    unit = await get_unit_or_404(session, unit_id)
    staff.require_unit(unit.id)
    own = await repo.own_account(session, unit.id, "telegram")
    before = {"botUsername": own.bot_username if own else None, "connected": own is not None}
    token = (body.bot_token or "").strip()
    account_id: uuid.UUID | None = None
    if token:
        username = await _telegram_get_me(token)
        if own is None:
            own = MessagingAccount(unit_id=unit.id, channel="telegram", provider="telegram", created_by=staff.id)
            session.add(own)
        own.api_key_enc = encrypt(token)
        own.bot_username = username
        own.api_key_masked = _mask_bot_token(token)
        own.is_active = True
        await session.flush()
        account_id = own.id
        action = "telegram_connect"
    else:
        if own is not None:
            account_id = own.id
            _retire_account(own, staff)
        action = "telegram_disconnect"
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=unit.id, country_id=unit.country_id, action=action, entity="unit", entity_id=unit.id, before=before, after={"botUsername": own.bot_username if token and own else None, "connected": bool(token)}, ip=meta.ip, request_id=meta.request_id)
    await invalidate_tree_caches(unit.id)
    await _reload_bot(account_id)
    return await unit_dto(session, unit)
