"""Patients: list/search/duplicates/create/update + geo reference data (regions/districts).

Rules (DOMAIN_RULES section 4): phone normalised to `998XXXXXXXXX`; identity uniqueness per company
(passport case-insensitive, then PINFL, then phone only when neither passport nor PINFL is given);
discountPercent clamped 0..100; every mutation audited. Other modules use `get_patient_or_404`
and `patient_out`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.pagination import page_of, paginate_query, sort_clause
from app.core.schemas import Page, PageQuery
from app.core.textutil import digits, is_valid_uz_phone, norm_phone
from app.infrastructure.db.models import District, Patient, Region
from app.infrastructure.redis import cache
from app.modules.patients import repository as repo
from app.modules.patients.schemas import (
    DistrictOut,
    PatientAddress,
    PatientDuplicatesIn,
    PatientOut,
    PatientPatchIn,
    PatientPortalOut,
    PatientStatsOut,
    PatientUpsertIn,
    RegionOut,
)

REF_TTL_SECONDS = 3600
_REGIONS_KEY = "ref:regions"
_DISTRICTS_KEY = "ref:districts:{scope}"

_DUP_BY_INDEX = {
    "uq_patients_company_passport_alive": ("duplicate_passport", "Bu passport raqami bilan bemor mavjud"),
    "uq_patients_company_pinfl_alive": ("duplicate_pinfl", "Bu JSHSHIR bilan bemor mavjud"),
    "uq_patients_company_phone_noid_alive": ("duplicate_phone", "Bu telefon raqami bilan bemor mavjud"),
}


# ----------------------------------------------------------------------------- projections


def patient_out(p: Patient) -> PatientOut:
    """ORM row → frontend `Patient` shape (address/stats/portal always objects)."""
    return PatientOut(
        id=str(p.id),
        company_id=str(p.company_id),
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
        stats=PatientStatsOut(
            orders=p.stats_orders, last_visit_at=p.stats_last_visit_at, total_spent=p.stats_total_spent
        ),
        portal=PatientPortalOut(linked=p.portal_linked, telegram_chat_id=p.telegram_chat_id),
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
    }


def scope_company(staff: StaffPrincipal) -> uuid.UUID | None:
    """Company filter for id-addressed endpoints: superadmin sees every tenant, staff only their own."""
    return None if staff.is_super_admin else staff.company_id


# ----------------------------------------------------------------------------- reads


async def get_patient_or_404(
    session: AsyncSession, patient_id: uuid.UUID | str, company_id: uuid.UUID | str | None = None
) -> Patient:
    """Alive patient by id (optionally company-scoped) or 404 "Bemor topilmadi"."""
    try:
        pid = uuid.UUID(str(patient_id))
        cid = uuid.UUID(str(company_id)) if company_id else None
    except ValueError as exc:
        raise NotFoundError("Bemor topilmadi") from exc
    p = await repo.get_by_id(session, pid, cid)
    if p is None:
        raise NotFoundError("Bemor topilmadi")
    return p


async def list_patients(
    session: AsyncSession, company_id: uuid.UUID, q: PageQuery, tag: str | None
) -> Page[PatientOut]:
    """Paged company patients; search predicate over name/phone/passport; optional exact tag; sortable whitelist."""
    stmt = repo.base_select(company_id)
    pred = repo.search_predicate(q.search)
    if pred is not None:
        stmt = stmt.where(pred)
    if tag:
        stmt = stmt.where(Patient.tags.any(tag))
    order = [sort_clause(q.sort_by, q.sort_dir, repo.SORTABLE, "createdAt"), Patient.id.desc()]
    rows, total = await paginate_query(session, stmt, q, order_by=order)
    return page_of([patient_out(p) for p in rows], q, total)


async def search_patients(session: AsyncSession, company_id: uuid.UUID, query: str, limit: int) -> list[PatientOut]:
    """Quick-pick list (empty query → most recent visitors)."""
    return [patient_out(p) for p in await repo.search(session, company_id, query, limit)]


async def find_duplicates(session: AsyncSession, company_id: uuid.UUID, body: PatientDuplicatesIn) -> list[PatientOut]:
    """Existing patients sharing phone / passport / PINFL with the draft (client filters itself out)."""
    phone = norm_phone(body.phone) if body.phone else None
    rows = await repo.find_duplicates(
        session, company_id, phone=phone or None, passport=_clean(body.passport_number), pinfl=_clean(body.pinfl)
    )
    return [patient_out(p) for p in rows]


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
    company_id: uuid.UUID,
    *,
    passport: str | None,
    pinfl: str | None,
    phone: str,
    exclude_id: uuid.UUID | None,
) -> None:
    """Order: duplicate_passport → duplicate_pinfl → duplicate_phone (only when neither passport nor pinfl)."""
    if passport and await repo.find_by_passport(session, company_id, passport, exclude_id):
        raise ConflictError("Bu passport raqami bilan bemor mavjud", code="duplicate_passport")
    if pinfl and await repo.find_by_pinfl(session, company_id, pinfl, exclude_id):
        raise ConflictError("Bu JSHSHIR bilan bemor mavjud", code="duplicate_pinfl")
    if not passport and not pinfl and await repo.find_by_phone(session, company_id, phone, exclude_id):
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
    session: AsyncSession, company_id: uuid.UUID, staff: StaffPrincipal, body: PatientUpsertIn, meta: RequestMeta
) -> PatientOut:
    """Create a patient in the company (tags [], stats zero, portal unlinked) — audited."""
    phone = _phone(body.phone)
    passport = _clean(body.passport_number)
    pinfl = _pinfl(body.pinfl)
    await _assert_identity_free(session, company_id, passport=passport, pinfl=pinfl, phone=phone, exclude_id=None)
    addr = body.address or PatientAddress()
    p = Patient(
        company_id=company_id,
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
        company_id=company_id,
        action="create",
        entity="patient",
        entity_id=p.id,
        after=_snapshot(p),
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return patient_out(p)


async def update_patient(
    session: AsyncSession, patient_id: uuid.UUID, staff: StaffPrincipal, body: PatientPatchIn, meta: RequestMeta
) -> PatientOut:
    """Partial update (only keys present in the body); identity uniqueness re-checked excluding self — audited."""
    p = await get_patient_or_404(session, patient_id, scope_company(staff))
    before = _snapshot(p)
    given = body.model_fields_set

    phone = _phone(body.phone) if "phone" in given and body.phone else p.phone
    passport = _clean(body.passport_number) if "passport_number" in given else p.passport_number
    pinfl = _pinfl(body.pinfl) if "pinfl" in given else p.pinfl
    await _assert_identity_free(session, p.company_id, passport=passport, pinfl=pinfl, phone=phone, exclude_id=p.id)

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
        company_id=p.company_id,
        action="update",
        entity="patient",
        entity_id=p.id,
        before=before,
        after=_snapshot(p),
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return patient_out(p)


# ----------------------------------------------------------------------------- reference data


def _region_out(r: Region) -> dict[str, Any]:
    return RegionOut(id=str(r.id), name=r.name).model_dump(by_alias=True)


def _district_out(d: District) -> dict[str, Any]:
    return DistrictOut(id=str(d.id), region_id=str(d.region_id), name=d.name).model_dump(by_alias=True)


async def list_regions(session: AsyncSession) -> list[dict[str, Any]]:
    """All regions (public reference data, cached 1h)."""

    async def load() -> list[dict[str, Any]]:
        return [_region_out(r) for r in await repo.list_regions(session)]

    return await cache.cached(_REGIONS_KEY, REF_TTL_SECONDS, load)


async def list_districts(session: AsyncSession, region_id: uuid.UUID | None) -> list[dict[str, Any]]:
    """Districts, optionally filtered by region (public reference data, cached 1h)."""

    async def load() -> list[dict[str, Any]]:
        return [_district_out(d) for d in await repo.list_districts(session, region_id)]

    return await cache.cached(_DISTRICTS_KEY.format(scope=region_id or "all"), REF_TTL_SECONDS, load)


async def invalidate_reference_cache() -> None:
    """Drop cached regions/districts — call after seeding or editing reference data."""
    await cache.delete(_REGIONS_KEY)
    await cache.delete_prefix(_DISTRICTS_KEY.format(scope=""))
