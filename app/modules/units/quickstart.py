"""Tezkor ishga tushirish — one request creates a facility that is ready to work.

In one transaction: the region / district nodes it needs when they do not exist yet, the facility itself, the
default staff accounts (kassir, vrach, laborant, rahbar — one password for all, which the people change
themselves later), the working set of a sample facility (the catalogue it uses and the templates bound to it,
re-bound to the new facility) and an active cheque template. The preview (`GET`) tells beforehand what the new
facility inherits from the units above it, which facilities can serve as the sample, and the roles.

Catalogue rows (categories, services, result schemas) are owned by a unit and visible to its whole subtree; a row
the sample uses that is owned ABOVE the new facility too is simply inherited (same id). The other rows are copied
and re-owned by the `owner` level of the new facility (its region node by default — the production convention:
one catalogue per viloyat). Templates are bound to a facility in practice (each SES has its own letterhead and
stamp), so every template usable at the sample is copied, remapped to the copied ids and bound to the new one.

Rights: the caller manages the parent (`admin.unit.manage`) and may create employees there (`admin.employee.write`);
copying a sample also needs `admin.catalog.write`, `admin.schema.write` and `admin.template.write`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.permissions import PROTECTED_ROLE_KEYS
from app.core.scope import ancestor_ids as scope_ancestor_ids
from app.core.scope import load_scope
from app.infrastructure.db.base import alive, uuid7
from app.infrastructure.db.models import (
    AttributeSchema,
    Category,
    OrgUnit,
    ResultTemplate,
    Role,
    ServiceType,
    TemplateUnit,
)
from app.modules.catalog import repository as catalog_repo
from app.modules.catalog import service as catalog_svc
from app.modules.staff import repository as staff_repo
from app.modules.staff import service as staff_svc
from app.modules.staff.schemas import EmployeeCreateIn
from app.modules.templates import repository as tpl_repo
from app.modules.templates import service as tpl_svc
from app.modules.templates.defaults import DEFAULT_RECEIPT_NAME, default_receipt_doc
from app.modules.units import repository as units_repo
from app.modules.units import service
from app.modules.units.schemas import (
    OrgUnitOut,
    QuickStartCatalogOut,
    QuickStartCounts,
    QuickStartEmployeeOut,
    QuickStartIn,
    QuickStartNodeOut,
    QuickStartOut,
    QuickStartPreviewOut,
    QuickStartReceiptOut,
    QuickStartRoleOut,
    QuickStartSampleOut,
    UnitCreateIn,
)

PLATFORM_MANAGE = "platform.unit.manage"
#: the default accounts, in the order the UI lists them: system role key → login suffix
DEFAULT_ROLES: tuple[tuple[str, str], ...] = (
    ("registrator", "kassa"),
    ("vrach", "vrach"),
    ("laborant", "laborant"),
    ("rahbar", "rahbar"),
    ("admin", "admin"),
)
COPY_PERMISSIONS = ("admin.catalog.write", "admin.schema.write", "admin.template.write")
_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")
LOGIN_INVALID = "Login 3–40 belgi: lotin harflari, raqamlar va . _ - (harf yoki raqam bilan boshlanadi)"
MSG_FACILITY_PARENT = "Muassasa ostida birlik yaratib bo‘lmaydi"
MSG_REGION_REQUIRED = "Viloyat tanlanmagan"
MSG_DISTRICT_REQUIRED = "Tuman / shahar tanlanmagan"
MSG_SAMPLE_NOT_FOUND = "Namuna muassasa topilmadi"


# ----------------------------------------------------------------------------- rights


def require_quick_start_right(staff: StaffPrincipal, parent: OrgUnit) -> None:
    """Manage the parent unit and create employees under it."""
    staff.require_unit(parent.id)
    if not (staff.has("admin.unit.manage", PLATFORM_MANAGE) and staff.has("admin.employee.write")):
        raise ForbiddenError("Ruxsat yo‘q")


def _require_copy_right(staff: StaffPrincipal) -> None:
    if not all(staff.has(p) for p in COPY_PERMISSIONS):
        raise ForbiddenError("Ruxsat yo‘q")


# ----------------------------------------------------------------------------- nodes


def _node_out(u: OrgUnit, path: str) -> QuickStartNodeOut:
    return QuickStartNodeOut(id=str(u.id), name=u.name, level=u.level, path=path)  # type: ignore[arg-type]


def _uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def _region_node(session: AsyncSession, parent: OrgUnit, region_id: uuid.UUID) -> OrgUnit | None:
    stmt = select(OrgUnit).where(
        OrgUnit.level == "region", OrgUnit.parent_id == parent.id, OrgUnit.region_id == region_id, alive(OrgUnit)
    )
    return (await session.execute(stmt)).scalars().first()


async def _district_node(session: AsyncSession, parent: OrgUnit, district_id: uuid.UUID) -> OrgUnit | None:
    stmt = select(OrgUnit).where(
        OrgUnit.level == "district", OrgUnit.parent_id == parent.id, OrgUnit.district_id == district_id, alive(OrgUnit)
    )
    return (await session.execute(stmt)).scalars().first()


async def _existing_chain(
    session: AsyncSession, parent: OrgUnit, region_id: str | None, district_id: str | None
) -> tuple[OrgUnit | None, OrgUnit | None]:
    """The region / district nodes that already exist for the chosen geo under `parent` (None = would be created)."""
    region = district = None
    if parent.level == "country":
        rid = _uuid(region_id)
        region = await _region_node(session, parent, rid) if rid else None
        did = _uuid(district_id)
        district = await _district_node(session, region, did) if region is not None and did else None
    elif parent.level == "region":
        region = parent
        did = _uuid(district_id)
        district = await _district_node(session, parent, did) if did else None
    elif parent.level == "district":
        district = parent
        region = await units_repo.get_unit(session, parent.parent_id) if parent.parent_id else None
    return region, district


def _grant(staff: StaffPrincipal, unit_id: uuid.UUID) -> None:
    """A node created in this very request is inside the caller's territory, but the principal's allowed set was
    computed when the request started — widen it so the next step (creating under the new node) passes."""
    if staff.allowed_unit_ids is not None:
        object.__setattr__(staff, "allowed_unit_ids", frozenset({*staff.allowed_unit_ids, unit_id}))


# ----------------------------------------------------------------------------- counting the catalogue


def _counts(
    cats: int = 0, services: int = 0, schemas: int = 0, templates: int = 0, receipts: int = 0
) -> QuickStartCounts:
    return QuickStartCounts(
        categories=cats, service_types=services, schemas=schemas, templates=templates, receipts=receipts
    )


async def _owned_counts(session: AsyncSession, owner_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, QuickStartCounts]:
    """Alive catalogue rows (categories, services, schemas) per owner unit."""
    out: dict[uuid.UUID, QuickStartCounts] = {}
    if not owner_ids:
        return out
    for model, attr in ((Category, "categories"), (ServiceType, "service_types"), (AttributeSchema, "schemas")):
        rows = await session.execute(
            select(model.owner_unit_id, func.count())
            .where(model.owner_unit_id.in_(owner_ids), alive(model))
            .group_by(model.owner_unit_id)
        )
        for owner, n in rows.all():
            cur = out.setdefault(owner, QuickStartCounts())
            setattr(cur, attr, int(n))
    return out


async def _templates_usable(session: AsyncSession, path: Sequence[uuid.UUID]) -> list[ResultTemplate]:
    """Templates (any scope, not archived) usable at the unit whose root → unit path is `path`."""
    rows = await tpl_repo.list_templates(session, list(path), path_ids=list(path))
    return [t for t in rows if t.status != "archived"]


def _template_counts(rows: Iterable[ResultTemplate]) -> tuple[int, int]:
    docs = receipts = 0
    for t in rows:
        if t.scope == "receipt":
            receipts += 1
        else:
            docs += 1
    return docs, receipts


async def _inherits(session: AsyncSession, ancestors: Sequence[uuid.UUID]) -> tuple[QuickStartCounts, list[str]]:
    """What a facility under `ancestors` (root → parent) sees without copying anything."""
    owned = await _owned_counts(session, ancestors)
    # templates with no bindings (usable everywhere under their owner) — bound ones belong to other facilities
    templates = await tpl_repo.list_templates(session, list(ancestors))
    bound = await tpl_repo.template_unit_ids(session, [t.id for t in templates])
    free = [t for t in templates if t.status != "archived" and not bound.get(t.id)]
    docs, receipts = _template_counts(free)
    total = _counts(
        sum(c.categories for c in owned.values()),
        sum(c.service_types for c in owned.values()),
        sum(c.schemas for c in owned.values()),
        docs,
        receipts,
    )
    names = await units_repo.units_by_ids(
        session, [o for o in ancestors if o in owned or any(t.owner_unit_id == o for t in free)]
    )
    return total, [names[o].name for o in ancestors if o in names]


async def _samples(
    session: AsyncSession, staff: StaffPrincipal, new_ancestors: set[uuid.UUID]
) -> list[QuickStartSampleOut]:
    """Alive facilities the caller may see that have templates bound to them or a catalogue on their path."""
    visible = service.visible_ids_for(staff)
    stmt = select(OrgUnit).where(OrgUnit.level == "facility", alive(OrgUnit), OrgUnit.is_active.is_(True))
    if visible is not None:
        context = await units_repo.ancestors_of(session, staff.unit_ids)
        stmt = stmt.where(OrgUnit.id.in_([*visible, *context]))
    facilities = (await session.execute(stmt.order_by(OrgUnit.sort_order.asc(), OrgUnit.name.asc()))).scalars().all()
    if not facilities:
        return []
    paths = {f.id: await scope_ancestor_ids(session, f.id) for f in facilities}
    owners = {o for p in paths.values() for o in p}
    owned = await _owned_counts(session, list(owners))
    templates = [t for t in await tpl_repo.list_templates(session, list(owners)) if t.status != "archived"]
    bound = await tpl_repo.template_unit_ids(session, [t.id for t in templates])
    names = await units_repo.paths(session, [f.id for f in facilities])
    out: list[QuickStartSampleOut] = []
    for f in facilities:
        path = paths[f.id]
        on_path = set(path)
        usable = [
            t
            for t in templates
            if t.owner_unit_id in on_path and (not bound.get(t.id) or any(u in on_path for u in bound[t.id]))
        ]
        docs, receipts = _template_counts(usable)
        cats = sum(owned[o].categories for o in path if o in owned)
        services = sum(owned[o].service_types for o in path if o in owned)
        schemas = sum(owned[o].schemas for o in path if o in owned)
        if not (docs or receipts or services):
            continue
        catalogue_owners = {
            o for o in path if o in owned and (owned[o].categories or owned[o].service_types or owned[o].schemas)
        }
        out.append(
            QuickStartSampleOut(
                id=str(f.id),
                name=f.name,
                code=f.code,
                kind=f.kind,
                path=names.get(f.id, f.name),  # type: ignore[arg-type]
                counts=_counts(cats, services, schemas, docs, receipts),
                same_catalogue=bool(catalogue_owners) and catalogue_owners <= new_ancestors,
            )
        )
    out.sort(key=lambda s: (-(s.counts.templates + s.counts.service_types), s.name))
    return out


async def _system_roles(session: AsyncSession) -> dict[str, Role]:
    rows = (
        (
            await session.execute(
                select(Role).where(
                    Role.scope_unit_id.is_(None), Role.key.in_([k for k, _ in DEFAULT_ROLES]), alive(Role)
                )
            )
        )
        .scalars()
        .all()
    )
    return {r.key: r for r in rows}


# ----------------------------------------------------------------------------- preview


async def preview(
    session: AsyncSession, parent: OrgUnit, region_id: str | None, district_id: str | None, staff: StaffPrincipal
) -> QuickStartPreviewOut:
    require_quick_start_right(staff, parent)
    if parent.level == "facility":
        raise ValidationError(MSG_FACILITY_PARENT, code="leaf")
    region, district = await _existing_chain(session, parent, region_id, district_id)
    deepest = district or region or parent
    ancestors = await scope_ancestor_ids(session, deepest.id)
    inherits, inherits_from = await _inherits(session, ancestors)
    paths = await units_repo.paths(session, [u.id for u in (parent, region, district) if u is not None])
    roles = await _system_roles(session)
    return QuickStartPreviewOut(
        parent=_node_out(parent, paths.get(parent.id, parent.name)),
        region_node=_node_out(region, paths.get(region.id, region.name))
        if region is not None and region.id != parent.id
        else None,
        district_node=_node_out(district, paths.get(district.id, district.name))
        if district is not None and district.id != parent.id
        else None,
        inherits=inherits,
        inherits_from=inherits_from,
        samples=await _samples(session, staff, set(ancestors)),
        roles=[
            QuickStartRoleOut(key=k, id=str(roles[k].id), name=roles[k].name, suffix=suffix)
            for k, suffix in DEFAULT_ROLES
            if k in roles
        ],  # type: ignore[arg-type]
        default_password=staff_svc.DEFAULT_PASSWORD,
    )


# ----------------------------------------------------------------------------- the copy


class _Copied:
    def __init__(self) -> None:
        self.categories: dict[uuid.UUID, uuid.UUID] = {}
        self.services: dict[uuid.UUID, uuid.UUID] = {}
        self.schemas: dict[uuid.UUID, uuid.UUID] = {}
        self.templates: dict[uuid.UUID, uuid.UUID] = {}
        self.inherited = QuickStartCounts()
        self.copied = QuickStartCounts()


async def copy_working_set(
    session: AsyncSession, sample: OrgUnit, facility: OrgUnit, owner: OrgUnit, staff: StaffPrincipal
) -> _Copied:
    """Copy what `sample` works with onto `facility`: catalogue rows not inherited by the facility are re-owned by
    `owner`; every template usable at the sample is copied, remapped and bound to the facility."""
    res = _Copied()
    sample_path = await scope_ancestor_ids(session, sample.id)
    new_path = set(await scope_ancestor_ids(session, facility.id))
    cats = await catalog_repo.list_categories(session, sample_path)
    services = await catalog_repo.list_service_types(session, sample_path)
    schemas = await catalog_repo.list_schemas(session, sample_path)
    templates = await _templates_usable(session, sample_path)

    # schemas first — services point at them
    for s in schemas:
        if s.owner_unit_id in new_path:
            res.schemas[s.id] = s.id
            res.inherited.schemas += 1
            continue
        if s.status == "archived":
            continue
        row = AttributeSchema(
            id=uuid7(),
            owner_unit_id=owner.id,
            name=s.name,
            description=s.description,
            version=1,
            status=s.status,
            fields=list(s.fields or []),
            created_by=staff.id,
        )
        session.add(row)
        res.schemas[s.id] = row.id
        res.copied.schemas += 1
    await session.flush()

    # categories keep their tree: parents before children
    pending = []
    for c in cats:
        if c.owner_unit_id in new_path:
            res.categories[c.id] = c.id
            res.inherited.categories += 1
        else:
            pending.append(c)
    while pending:
        progressed = False
        rest = []
        for c in pending:
            if c.parent_id is None or c.parent_id in res.categories:
                row = Category(
                    id=uuid7(),
                    owner_unit_id=owner.id,
                    parent_id=res.categories.get(c.parent_id) if c.parent_id else None,
                    name=c.name,
                    code=c.code,
                    icon=c.icon,
                    color=c.color,
                    sort_order=c.sort_order,
                    is_active=c.is_active,
                    phone=c.phone,
                    workflow=c.workflow,
                    created_by=staff.id,
                )
                session.add(row)
                res.categories[c.id] = row.id
                res.copied.categories += 1
                progressed = True
            else:
                rest.append(c)
        if not progressed:  # parents outside the sample's path: attach at the top
            for c in rest:
                row = Category(
                    id=uuid7(),
                    owner_unit_id=owner.id,
                    parent_id=None,
                    name=c.name,
                    code=c.code,
                    icon=c.icon,
                    color=c.color,
                    sort_order=c.sort_order,
                    is_active=c.is_active,
                    phone=c.phone,
                    workflow=c.workflow,
                    created_by=staff.id,
                )
                session.add(row)
                res.categories[c.id] = row.id
                res.copied.categories += 1
            rest = []
        pending = rest
    await session.flush()

    copied_services: list[tuple[ServiceType, ServiceType]] = []
    for s in services:
        if s.owner_unit_id in new_path:
            res.services[s.id] = s.id
            res.inherited.service_types += 1
            continue
        category_id = res.categories.get(s.category_id)
        if category_id is None:
            continue
        row = ServiceType(
            id=uuid7(),
            owner_unit_id=owner.id,
            category_id=category_id,
            name=s.name,
            code=s.code,
            description=s.description,
            price=s.price,
            turnaround_days=s.turnaround_days,
            sort_order=s.sort_order,
            is_active=s.is_active,
            schema_id=res.schemas.get(s.schema_id) if s.schema_id else None,
            document_scope=s.document_scope,
            default_template_id=None,
            created_by=staff.id,
        )
        session.add(row)
        res.services[s.id] = row.id
        res.copied.service_types += 1
        copied_services.append((s, row))
    await session.flush()

    bind_to = [facility.id] if owner.id != facility.id else []
    for t in templates:
        row = ResultTemplate(
            id=uuid7(),
            owner_unit_id=owner.id,
            name=t.name,
            description=t.description,
            status=t.status,
            version=1,
            service_type_ids=[res.services[x] for x in (t.service_type_ids or []) if x in res.services],
            category_ids=[res.categories[x] for x in (t.category_ids or []) if x in res.categories],
            scope=t.scope,
            language=t.language,
            doc=dict(t.doc or {}),
            thumbnail_url=t.thumbnail_url,
            usage=0,
            created_by=staff.id,
        )
        session.add(row)
        res.templates[t.id] = row.id
        if t.scope == "receipt":
            res.copied.receipts += 1
        else:
            res.copied.templates += 1
    await session.flush()
    for new_id in res.templates.values():
        for unit_id in bind_to:
            session.add(TemplateUnit(template_id=new_id, unit_id=unit_id))
    for src, row in copied_services:
        if src.default_template_id and src.default_template_id in res.templates:
            row.default_template_id = res.templates[src.default_template_id]
    await session.flush()
    await catalog_svc.invalidate_catalog_cache(owner.id)
    await tpl_svc.invalidate_template_cache(owner.id)
    return res


# ----------------------------------------------------------------------------- the cheque


async def ensure_active_receipt(
    session: AsyncSession, facility: OrgUnit, owner: OrgUnit, staff: StaffPrincipal
) -> ResultTemplate | None:
    """An active cheque template usable at the facility: the one already there, else the newest draft activated,
    else the standard cheque created active (owned by `owner`, bound to the facility)."""
    path = await scope_ancestor_ids(session, facility.id)
    usable = [t for t in await _templates_usable(session, path) if t.scope == "receipt"]
    active = [t for t in usable if t.status == "active"]
    if active:
        return active[0]
    drafts = [t for t in usable if t.status == "draft" and (t.doc or {}).get("elements")]
    if drafts:
        drafts[0].status = "active"
        await session.flush()
        await tpl_svc.invalidate_template_cache(drafts[0].owner_unit_id)
        return drafts[0]
    lang = await tpl_svc._receipt_language(session, facility.id, None)
    row = ResultTemplate(
        owner_unit_id=owner.id,
        name=DEFAULT_RECEIPT_NAME[lang],
        status="active",
        version=1,
        service_type_ids=[],
        category_ids=[],
        scope="receipt",
        language=lang,
        doc=default_receipt_doc("Receipt80", lang),
        usage=0,
        created_by=staff.id,
    )
    session.add(row)
    await session.flush()
    if owner.id != facility.id:
        session.add(TemplateUnit(template_id=row.id, unit_id=facility.id))
        await session.flush()
    await tpl_svc.invalidate_template_cache(owner.id)
    return row


# ----------------------------------------------------------------------------- staff


def _normalize_login(raw: str) -> str:
    login = raw.strip().lower()
    if not _LOGIN_RE.match(login):
        raise ValidationError(LOGIN_INVALID, code="invalid_login")
    return login


async def _free_login(session: AsyncSession, wanted: str, taken: set[str]) -> str:
    """`wanted`, or `wanted2`, `wanted3` … when it is already used (by anyone, or earlier in this request)."""
    base = wanted[:38]
    for n in range(1, 100):
        candidate = base if n == 1 else f"{base}{n}"
        if candidate not in taken and not await staff_repo.login_taken(session, candidate, None):
            taken.add(candidate)
            return candidate
    raise ConflictError("Bu login band")


# ----------------------------------------------------------------------------- the run


def _owner_for(choice: str, region: OrgUnit | None, district: OrgUnit | None, facility: OrgUnit) -> OrgUnit:
    if choice == "region" and region is not None:
        return region
    if choice in ("region", "district") and district is not None:
        return district
    return facility


async def quick_start(
    session: AsyncSession, parent: OrgUnit, body: QuickStartIn, staff: StaffPrincipal, meta: RequestMeta
) -> QuickStartOut:
    require_quick_start_right(staff, parent)
    if parent.level == "facility":
        raise ValidationError(MSG_FACILITY_PARENT, code="leaf")
    if body.sample is not None:
        _require_copy_right(staff)
    sample: OrgUnit | None = None
    if body.sample is not None:
        sample = await units_repo.get_unit(session, _uuid(body.sample.facility_id) or uuid.uuid4())
        if sample is None or sample.level != "facility":
            raise NotFoundError(MSG_SAMPLE_NOT_FOUND)
        await service.require_visible(session, staff, sample)
    roles = await _system_roles(session)
    for s in body.staff:
        if s.role not in roles or s.role in PROTECTED_ROLE_KEYS:
            raise NotFoundError("Rol topilmadi")

    # 1. the chain: region / district nodes that are missing
    region, district = await _existing_chain(session, parent, body.region_id, body.district_id)
    created_region = created_district = None
    if parent.level == "country":
        if not body.region_id:
            raise ValidationError(MSG_REGION_REQUIRED, code="region_required")
        if region is None:
            geo = await units_repo.get_region(session, _uuid(body.region_id) or uuid.uuid4())
            if geo is None:
                raise ValidationError("Viloyat topilmadi")
            dto = await service.create_unit(
                session,
                UnitCreateIn(parent_id=str(parent.id), region_id=body.region_id, name=body.region_name or geo.name),
                staff,
                meta,
            )
            region = created_region = await service.get_unit_or_404(session, dto.id)
            _grant(staff, region.id)
    if parent.level in ("country", "region"):
        if not body.district_id:
            raise ValidationError(MSG_DISTRICT_REQUIRED, code="district_required")
        assert region is not None
        if district is None:
            geo_d = await units_repo.get_district(session, _uuid(body.district_id) or uuid.uuid4())
            if geo_d is None:
                raise ValidationError("Tuman topilmadi")
            dto = await service.create_unit(
                session,
                UnitCreateIn(
                    parent_id=str(region.id), district_id=body.district_id, name=body.district_name or geo_d.name
                ),
                staff,
                meta,
            )
            district = created_district = await service.get_unit_or_404(session, dto.id)
            _grant(staff, district.id)
    assert district is not None

    # 2. the facility
    f = body.facility
    facility_dto = await service.create_unit(
        session,
        UnitCreateIn(
            parent_id=str(district.id),
            kind=f.kind,
            name=f.name,
            short_name=f.short_name,
            code=f.code,
            legal_name=f.legal_name,
            phone=f.phone,
            email=f.email,
            address=f.address,
            timezone=f.timezone,
        ),
        staff,
        meta,
    )
    facility = await service.get_unit_or_404(session, facility_dto.id)
    _grant(staff, facility.id)
    owner = _owner_for(body.sample.owner if body.sample else "region", region, district, facility)

    # 3. the working set of the sample, then the cheque
    ancestors = await scope_ancestor_ids(session, district.id)
    inherited, _ = await _inherits(session, ancestors)
    catalog = QuickStartCatalogOut(inherited=inherited, copied=QuickStartCounts())
    if sample is not None:
        res = await copy_working_set(session, sample, facility, owner, staff)
        catalog = QuickStartCatalogOut(
            sample_name=sample.name, owner_name=owner.name, inherited=inherited, copied=res.copied
        )
    receipt = await ensure_active_receipt(session, facility, owner, staff) if body.activate_receipt else None

    # 4. the people
    scope = await load_scope(session, facility.id)
    assert scope is not None
    employees: list[QuickStartEmployeeOut] = []
    taken: set[str] = set()
    for s in body.staff:
        login = await _free_login(session, _normalize_login(s.login), taken)
        role = roles[s.role]
        emp = await staff_svc.create_employee(
            session,
            scope,
            EmployeeCreateIn(
                full_name=s.full_name,
                login=login,
                password=body.password,
                role_id=str(role.id),
                unit_id=str(facility.id),
            ),
            staff,
            meta,
        )
        employees.append(
            QuickStartEmployeeOut(
                id=emp.id,
                full_name=emp.full_name,
                login=emp.login,
                role_key=role.key,
                role_name=role.name,
                password=body.password,
            )
        )

    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=facility.id,
        country_id=facility.country_id,
        action="quick_start",
        entity="unit",
        entity_id=facility.id,
        after={
            "facility": facility.name,
            "code": facility.code,
            "region_created": created_region.name if created_region else None,
            "district_created": created_district.name if created_district else None,
            "staff": [{"login": e.login, "role": e.role_key} for e in employees],
            "sample": sample.name if sample else None,
            "owner": owner.name,
            "copied": catalog.copied.model_dump(),
            "receipt": str(receipt.id) if receipt else None,
        },
        ip=meta.ip,
        request_id=meta.request_id,
    )
    await service.invalidate_tree_caches(facility.id)
    return QuickStartOut(
        facility=await _fresh_dto(session, facility),
        region=await _fresh_dto(session, created_region) if created_region else None,
        district=await _fresh_dto(session, created_district) if created_district else None,
        employees=employees,
        catalog=catalog,
        receipt=QuickStartReceiptOut(id=str(receipt.id), name=receipt.name, status=receipt.status) if receipt else None,
    )


async def _fresh_dto(session: AsyncSession, unit: OrgUnit) -> OrgUnitOut:
    """The unit card after everything was added under it (counts changed since `create_unit` cached it)."""
    from app.infrastructure.redis import cache

    await cache.delete(service._dto_key(unit.id))
    return await service.unit_dto(session, unit)
