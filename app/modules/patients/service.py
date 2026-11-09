"""Patients: list/search/duplicates/create/update + geo reference data (countries/regions/districts).

Rules (DOMAIN_RULES section 4): phone normalised to `998XXXXXXXXX`; identity uniqueness per country
(passport case-insensitive, then PINFL, then phone only when neither passport nor PINFL is given);
discountPercent clamped 0..100; every mutation audited. Lists are scoped by the organisation unit
(`registered_unit_id` or an order inside the unit's subtree), identity is national.
Other modules use `get_patient_or_404`, `patient_out` and `patient_dto`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.pagination import page_of, paginate_query, sort_clause
from app.core.schemas import Page
from app.core.scope import UnitScope, ids_of, load_scope
from app.core.textutil import digits, is_valid_uz_phone, norm_phone
from app.infrastructure.db.models import Country, District, Patient, Region
from app.infrastructure.redis import cache
from app.modules.patients import repository as repo
from app.modules.patients.schemas import (
    CountryOut,
    DistrictOut,
    PatientAddress,
    PatientDuplicatesIn,
    PatientListQuery,
    PatientOut,
    PatientPatchIn,
    PatientPortalOut,
    PatientStatsOut,
    PatientUpsertIn,
    RegionOut,
)
from app.modules.units import repository as units_repo
from app.modules.units import service as units_service

REF_TTL_SECONDS = 3600
_COUNTRIES_KEY = "ref:countries"
_DEFAULT_COUNTRY_KEY = "ref:country:default"
_REGIONS_KEY = "ref:regions:{scope}"
_DISTRICTS_KEY = "ref:districts:{scope}"

_DUP_BY_INDEX = {
    "uq_patients_country_passport_alive": ("duplicate_passport", "Bu passport raqami bilan bemor mavjud"),
    "uq_patients_country_pinfl_alive": ("duplicate_pinfl", "Bu JSHSHIR bilan bemor mavjud"),
    "uq_patients_country_phone_noid_alive": ("duplicate_phone", "Bu telefon raqami bilan bemor mavjud"),
}

#: an `IN (...)` list that matches nothing (an unknown / foreign sub-unit was asked for)
_NO_UNITS: list[uuid.UUID] = [uuid.UUID(int=0)]


# ----------------------------------------------------------------------------- projections


def patient_out(p: Patient, stats: tuple[int, datetime | None, int] | None = None, unit_name: str | None = None) -> PatientOut:
    """ORM row → frontend `Patient` shape (address/stats/portal always objects).

    `stats` = (orders, last visit, total spent) counted inside the caller's unit scope; default: country-wide.
    `unit_name` = the registering facility's name (resolved by `patient_dto` / the list helpers)."""
    orders, last_visit, spent = stats if stats is not None else (p.stats_orders, p.stats_last_visit_at, p.stats_total_spent)
    return PatientOut(
        id=str(p.id),
        country_id=str(p.country_id),
        registered_unit_id=str(p.registered_unit_id) if p.registered_unit_id else None,
        registered_unit_name=unit_name,
        full_name=p.full_name,
        phone=p.phone,
        phone_extra=p.phone_extra,
        gender=p.gender,  # type: ignore[arg-type]
        birth_date=p.birth_date,
        passport_number=p.passport_number,
        pinfl=p.pinfl,
        address=PatientAddress(
            region_id=str(p.region_id) if p.region_id else None,
            district_id=str(p.district_id) if p.district_id else None,
            street=p.street,
        ),
        workplace=p.workplace,
        discount_percent=p.discount_percent,
        contract_number=p.contract_number,
        note=p.note,
        tags=list(p.tags or []),
        stats=PatientStatsOut(orders=orders, last_visit_at=last_visit, total_spent=spent),
        portal=PatientPortalOut(
            linked=p.portal_linked,
            telegram_chat_id=p.telegram_chat_id,
            login=p.portal_login if p.portal_password_hash else None,
            login_set_at=p.portal_password_set_at if p.portal_password_hash else None,
        ),
        created_at=p.created_at,
        updated_at=p.updated_at,
        created_by=str(p.created_by) if p.created_by else None,
    )


def _snapshot(p: Patient) -> dict[str, Any]:
    """Small before/after dict for the audit log (identity + editable fields only)."""
    return {
        "fullName": p.full_name,
        "phone": p.phone,
        "phoneExtra": p.phone_extra,
        "gender": p.gender,
        "birthDate": p.birth_date.isoformat() if p.birth_date else None,
        "passportNumber": p.passport_number,
        "pinfl": p.pinfl,
        "regionId": str(p.region_id) if p.region_id else None,
        "districtId": str(p.district_id) if p.district_id else None,
        "street": p.street,
        "workplace": p.workplace,
        "discountPercent": p.discount_percent,
        "contractNumber": p.contract_number,
        "note": p.note,
        "tags": list(p.tags or []),
        "registeredUnitId": str(p.registered_unit_id) if p.registered_unit_id else None,
    }


async def _outs(session: AsyncSession, rows: Sequence[Patient], unit_ids: Sequence[uuid.UUID] | None) -> list[PatientOut]:
    """DTOs with the registering facility's name and the visit statistics of the unit scope (country-wide when None)."""
    if not rows:
        return []
    units = await units_repo.units_by_ids(session, [p.registered_unit_id for p in rows])
    names = {uid: u.name for uid, u in units.items()}
    if unit_ids is None:
        return [patient_out(p, None, names.get(p.registered_unit_id)) for p in rows]
    stats = await repo.unit_stats(session, [p.id for p in rows], unit_ids)
    return [patient_out(p, stats.get(p.id, (0, None, 0)), names.get(p.registered_unit_id)) for p in rows]


async def patient_dto(session: AsyncSession, p: Patient, unit_ids: Sequence[uuid.UUID] | None = None) -> PatientOut:
    """One loaded patient as a DTO (facility name resolved; statistics inside `unit_ids` when given)."""
    return (await _outs(session, [p], unit_ids))[0]


# ----------------------------------------------------------------------------- scope helpers


def _visible_units(staff: StaffPrincipal) -> list[uuid.UUID] | None:
    """The units an id-addressed request may touch: None = everything (superadmin), else the allowed set."""
    if staff.is_super_admin or staff.allowed_unit_ids is None:
        return None
    return list(staff.allowed_unit_ids)


async def _scope_units(
    session: AsyncSession, scope: UnitScope, staff: StaffPrincipal, unit_id: uuid.UUID | str | None
) -> list[uuid.UUID]:
    """The units a list request covers: the scope's whole subtree, or — with a `unitId` narrowing — the subtree
    of that sub-unit (an id outside the scope matches nothing)."""
    if unit_id in (None, ""):
        return staff.restrict(scope.descendant_ids)
    ids = ids_of([unit_id])
    if not ids or not scope.contains(ids[0]):
        return _NO_UNITS
    sub = await load_scope(session, ids[0])
    return staff.restrict(sub.descendant_ids if sub is not None else ids)


# ----------------------------------------------------------------------------- reads


async def get_patient_or_404(
    session: AsyncSession, patient_id: uuid.UUID | str, staff: StaffPrincipal, *, unit_ids: Sequence[uuid.UUID] | None = None
) -> Patient:
    """Alive patient by id or 404 "Bemor topilmadi". A superadmin sees every patient; other staff only those
    registered at / seen in their allowed units (`unit_ids` narrows that further, e.g. a route's scope)."""
    try:
        pid = uuid.UUID(str(patient_id))
    except ValueError as exc:
        raise NotFoundError("Bemor topilmadi") from exc
    if staff.is_super_admin:
        units: list[uuid.UUID] | None = None
    elif unit_ids is not None:
        units = staff.restrict(list(unit_ids))
    else:
        units = _visible_units(staff) or []
    p = await repo.get_by_id(session, pid, units)
    if p is None:
        raise NotFoundError("Bemor topilmadi")
    return p


async def get_patient_dto(
    session: AsyncSession, patient_id: uuid.UUID, staff: StaffPrincipal, unit_id: uuid.UUID | str | None = None
) -> PatientOut:
    """One patient (identity is country-wide) with the statistics of `unit_id`'s subtree when given, else of the
    caller's allowed units (country-wide for a superadmin)."""
    p = await get_patient_or_404(session, patient_id, staff)
    if unit_id:
        staff.require_unit(unit_id)
        units: list[uuid.UUID] | None = (await units_service.scope_or_404(session, unit_id)).descendant_ids
    else:
        units = _visible_units(staff)
    return await patient_dto(session, p, units)


async def list_patients(session: AsyncSession, scope: UnitScope, q: PatientListQuery, staff: StaffPrincipal) -> Page[PatientOut]:
    """Paged patients of the unit's subtree (registered there or seen there); search predicate over
    name/phone/passport; optional exact tag; optional `unitId` narrowing to a sub-unit; sortable whitelist."""
    units = await _scope_units(session, scope, staff, q.unit_id)
    stmt = repo.base_select(scope.country_id).where(repo.in_units(units))
    pred = repo.search_predicate(q.search)
    if pred is not None:
        stmt = stmt.where(pred)
    if q.tag:
        stmt = stmt.where(Patient.tags.any(q.tag))
    order = [sort_clause(q.sort_by, q.sort_dir, repo.SORTABLE, "createdAt"), Patient.id.desc()]
    rows, total = await paginate_query(session, stmt, q, order_by=order)
    return page_of(await _outs(session, list(rows), units), q, total)


async def search_patients(
    session: AsyncSession, scope: UnitScope, staff: StaffPrincipal, query: str, limit: int, unit_id: uuid.UUID | str | None = None
) -> list[PatientOut]:
    """Quick-pick list (empty query → the scope's most recent visitors). Identity is country-wide — a patient of
    another facility must be found here instead of being registered twice — but the statistics are the scope's."""
    units = await _scope_units(session, scope, staff, unit_id)
    return await _outs(session, await repo.search(session, scope.country_id, query, limit, units), units)


async def find_duplicates(session: AsyncSession, scope: UnitScope, body: PatientDuplicatesIn) -> list[PatientOut]:
    """Existing patients of the country sharing phone / passport / PINFL with the draft (client filters itself out)."""
    phone = norm_phone(body.phone) if body.phone else None
    rows = await repo.find_duplicates(
        session, scope.country_id, phone=phone or None, passport=_clean(body.passport_number), pinfl=_clean(body.pinfl)
    )
    return await _outs(session, list(rows), scope.descendant_ids)


# ----------------------------------------------------------------------------- writes


def _clean(v: str | None) -> str | None:
    s = (v or "").strip()
    return s or None


def _phone(raw: str) -> str:
    phone = norm_phone(raw)
    if not is_valid_uz_phone(phone):
        raise ValidationError("Telefon raqam noto‘g‘ri", code="invalid_phone")
    return phone


def _pinfl(raw: str | None) -> str | None:
    v = _clean(raw)
    if v is None:
        return None
    if len(v) != 14 or digits(v) != v:
        raise ValidationError("JSHSHIR 14 ta raqamdan iborat bo‘lishi kerak", code="invalid_pinfl")
    return v


def _uuid_or_none(v: str | None, label: str) -> uuid.UUID | None:
    if not v:
        return None
    try:
        return uuid.UUID(str(v))
    except ValueError as exc:
        raise ValidationError(f"{label} noto‘g‘ri") from exc


def _clamp_discount(v: int | None) -> int:
    return max(0, min(100, int(v or 0)))


async def _assert_identity_free(
    session: AsyncSession,
    country_id: uuid.UUID,
    *,
    passport: str | None,
    pinfl: str | None,
    phone: str,
    exclude_id: uuid.UUID | None,
) -> None:
    """Order: duplicate_passport → duplicate_pinfl → duplicate_phone (only when neither passport nor pinfl)."""
    if passport and await repo.find_by_passport(session, country_id, passport, exclude_id):
        raise ConflictError("Bu passport raqami bilan bemor mavjud", code="duplicate_passport")
    if pinfl and await repo.find_by_pinfl(session, country_id, pinfl, exclude_id):
        raise ConflictError("Bu JSHSHIR bilan bemor mavjud", code="duplicate_pinfl")
    if not passport and not pinfl and await repo.find_by_phone(session, country_id, phone, exclude_id):
        raise ConflictError("Bu telefon raqami bilan bemor mavjud", code="duplicate_phone")


async def _flush_identity(session: AsyncSession) -> None:
    """Flush and translate a lost race on the partial unique indexes into the same 409 codes."""
    try:
        await session.flush()
    except IntegrityError as exc:
        text = str(exc.orig)
        for index, (code, message) in _DUP_BY_INDEX.items():
            if index in text:
                raise ConflictError(message, code=code) from exc
        raise


async def create_patient(
    session: AsyncSession, scope: UnitScope, staff: StaffPrincipal, body: PatientUpsertIn, meta: RequestMeta
) -> PatientOut:
    """Register a patient in the scope's country at a facility (the path unit when it is one, else
    `registeredUnitId` — 422 `facility_required` otherwise); tags [], stats zero, portal unlinked — audited."""
    facility = await units_service.facility_or_422(session, scope, body.registered_unit_id)
    country_id = scope.country_id
    phone = _phone(body.phone)
    passport = _clean(body.passport_number)
    pinfl = _pinfl(body.pinfl)
    await _assert_identity_free(session, country_id, passport=passport, pinfl=pinfl, phone=phone, exclude_id=None)
    addr = body.address or PatientAddress()
    p = Patient(
        country_id=country_id,
        registered_unit_id=facility.id,
        full_name=body.full_name,
        phone=phone,
        phone_extra=norm_phone(body.phone_extra) if _clean(body.phone_extra) else None,
        gender=body.gender,
        birth_date=body.birth_date,
        passport_number=passport,
        pinfl=pinfl,
        region_id=_uuid_or_none(addr.region_id, "Viloyat"),
        district_id=_uuid_or_none(addr.district_id, "Tuman"),
        street=_clean(addr.street),
        workplace=_clean(body.workplace),
        discount_percent=_clamp_discount(body.discount_percent),
        contract_number=_clean(body.contract_number),
        note=_clean(body.note),
        tags=[t.strip() for t in (body.tags or []) if t and t.strip()],
        stats_orders=0,
        stats_total_spent=0,
        portal_linked=False,
        created_by=staff.id,
    )
    session.add(p)
    await _flush_identity(session)
    await session.refresh(p)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=facility.id,
        country_id=country_id,
        action="create",
        entity="patient",
        entity_id=p.id,
        after=_snapshot(p),
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return patient_out(p, None, facility.name)


async def update_patient(
    session: AsyncSession, patient_id: uuid.UUID, staff: StaffPrincipal, body: PatientPatchIn, meta: RequestMeta
) -> PatientOut:
    """Partial update (only keys present in the body); identity uniqueness re-checked excluding self — audited."""
    p = await get_patient_or_404(session, patient_id, staff)
    before = _snapshot(p)
    given = body.model_fields_set

    phone = _phone(body.phone) if "phone" in given and body.phone else p.phone
    passport = _clean(body.passport_number) if "passport_number" in given else p.passport_number
    pinfl = _pinfl(body.pinfl) if "pinfl" in given else p.pinfl
    await _assert_identity_free(session, p.country_id, passport=passport, pinfl=pinfl, phone=phone, exclude_id=p.id)

    p.phone, p.passport_number, p.pinfl = phone, passport, pinfl
    if "full_name" in given and body.full_name:
        p.full_name = body.full_name
    if "phone_extra" in given:
        p.phone_extra = norm_phone(body.phone_extra) if _clean(body.phone_extra) else None
    if "gender" in given:
        p.gender = body.gender
    if "birth_date" in given:
        p.birth_date = body.birth_date
    if "address" in given:
        addr = body.address or PatientAddress()
        p.region_id = _uuid_or_none(addr.region_id, "Viloyat")
        p.district_id = _uuid_or_none(addr.district_id, "Tuman")
        p.street = _clean(addr.street)
    if "workplace" in given:
        p.workplace = _clean(body.workplace)
    if "discount_percent" in given:
        p.discount_percent = _clamp_discount(body.discount_percent)
    if "contract_number" in given:
        p.contract_number = _clean(body.contract_number)
    if "note" in given:
        p.note = _clean(body.note)
    if "tags" in given:
        p.tags = [t.strip() for t in (body.tags or []) if t and t.strip()]

    await _flush_identity(session)
    await session.refresh(p)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=p.registered_unit_id,
        country_id=p.country_id,
        action="update",
        entity="patient",
        entity_id=p.id,
        before=before,
        after=_snapshot(p),
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return await patient_dto(session, p)


# ----------------------------------------------------------------------------- reference data


def _country_out(c: Country) -> dict[str, Any]:
    return CountryOut(
        id=str(c.id),
        code=c.code,
        name=c.name,
        name_ru=c.name_ru,
        name_en=c.name_en,
        phone_code=c.phone_code,
        currency_code=c.currency_code,
        default_locale=c.default_locale,
        timezone=c.timezone,
        phone_mask=c.phone_mask,
        is_enabled=c.is_enabled,
    ).model_dump(by_alias=True)


def _region_out(r: Region) -> dict[str, Any]:
    return RegionOut(id=str(r.id), country_id=str(r.country_id), name=r.name).model_dump(by_alias=True)


def _district_out(d: District) -> dict[str, Any]:
    kind = d.kind if d.kind in ("district", "city") else "district"
    return DistrictOut(id=str(d.id), region_id=str(d.region_id), name=d.name, kind=kind).model_dump(by_alias=True)  # type: ignore[arg-type]


async def list_countries(session: AsyncSession) -> list[dict[str, Any]]:
    """All countries (public reference data, cached 1h)."""

    async def load() -> list[dict[str, Any]]:
        return [_country_out(c) for c in await repo.list_countries(session)]

    return await cache.cached(_COUNTRIES_KEY, REF_TTL_SECONDS, load)


async def default_country_id(session: AsyncSession) -> uuid.UUID | None:
    """Id of `settings.default_country_code` (UZ) — the country `/regions` serves when none is asked for."""

    async def load() -> dict[str, Any]:
        c = await repo.country_by_code(session, settings.default_country_code)
        return {"id": str(c.id) if c else None}

    hit = await cache.cached(_DEFAULT_COUNTRY_KEY, REF_TTL_SECONDS, load)
    return uuid.UUID(hit["id"]) if hit.get("id") else None


async def list_regions(session: AsyncSession, country_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    """Regions of a country (default: the platform's default country), public, cached 1h."""
    scope = country_id or await default_country_id(session)

    async def load() -> list[dict[str, Any]]:
        return [_region_out(r) for r in await repo.list_regions(session, scope)]

    return await cache.cached(_REGIONS_KEY.format(scope=scope or "all"), REF_TTL_SECONDS, load)


async def list_districts(session: AsyncSession, region_id: uuid.UUID | None) -> list[dict[str, Any]]:
    """Districts, optionally filtered by region (public reference data, cached 1h)."""

    async def load() -> list[dict[str, Any]]:
        return [_district_out(d) for d in await repo.list_districts(session, region_id)]

    return await cache.cached(_DISTRICTS_KEY.format(scope=region_id or "all"), REF_TTL_SECONDS, load)


async def invalidate_reference_cache() -> None:
    """Drop cached countries/regions/districts — call after seeding or editing reference data."""
    await cache.delete(_COUNTRIES_KEY)
    await cache.delete(_DEFAULT_COUNTRY_KEY)
    await cache.delete_prefix(_REGIONS_KEY.format(scope=""))
    await cache.delete_prefix(_DISTRICTS_KEY.format(scope=""))
