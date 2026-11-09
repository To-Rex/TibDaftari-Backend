"""Patient portal — read-only self-service surface (DOMAIN_RULES section 11).

Identity: a patient may have been registered at several facilities under the same phone; the portal principal
therefore owns every alive patient row of its country sharing its phone (computed once per request) — except a
session opened with a portal login + password, which owns its own patient row only.
Ownership checks answer 404 (never 403) so foreign ids do not leak existence.
Item results are redacted: `values`/`labNote` are exposed only for approved items.
Places: a cheque names the facility it was written at (`unit`); the facility's organisation (`org`) is the nearest
non-facility unit above it — the letterhead of its documents.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PatientPrincipal
from app.core.exceptions import NotFoundError
from app.core.scope import ancestor_ids
from app.infrastructure.db.models import Order, OrderItem, OrgUnit, ResultDocument
from app.infrastructure.db.models import Patient as PatientModel
from app.modules.catalog import service as catalog_svc
from app.modules.files.service import file_url
from app.modules.orders import service as orders_svc
from app.modules.orders.schemas import OrderItemOut, ResultDocumentOut
from app.modules.portal import repository as repo
from app.modules.portal.schemas import (
    PortalAddressOut,
    PortalDocumentOut,
    PortalLinkOut,
    PortalOrderOut,
    PortalOrgOut,
    PortalOverviewOut,
    PortalPatientOut,
    PortalStatsOut,
    PortalUnitOut,
)
from app.modules.templates import service as templates_svc
from app.modules.units import repository as units_repo

ORDER_NOT_FOUND = "Chek topilmadi"
DOC_NOT_FOUND = "Hujjat topilmadi"


# ----------------------------------------------------------------------------- projections


def patient_out(p: PatientModel) -> PortalPatientOut:
    """Safe patient projection: identity documents, note, discount and tags are neutralised."""
    return PortalPatientOut(
        id=str(p.id),
        country_id=str(p.country_id),
        registered_unit_id=str(p.registered_unit_id) if p.registered_unit_id else None,
        full_name=p.full_name,
        phone=p.phone,
        gender=p.gender,  # type: ignore[arg-type]
        birth_date=p.birth_date,
        address=PortalAddressOut(
            region_id=str(p.region_id) if p.region_id else None,
            district_id=str(p.district_id) if p.district_id else None,
            street=p.street,
        ),
        discount_percent=0,
        tags=[],
        stats=PortalStatsOut(orders=p.stats_orders, last_visit_at=p.stats_last_visit_at, total_spent=p.stats_total_spent),
        portal=PortalLinkOut(linked=p.portal_linked, telegram_chat_id=p.telegram_chat_id),
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def org_out(org: OrgUnit | None, org_id: uuid.UUID) -> PortalOrgOut:
    """Public organisation card; a facility without a known organisation degrades to an id-only card."""
    if org is None:
        return PortalOrgOut(id=str(org_id), name="")
    return PortalOrgOut(
        id=str(org.id),
        name=org.name,
        logo_url=file_url(org.logo_file_id) if org.logo_file_id else None,
        phone=org.phone,
        address=org.address,
    )


def unit_out(u: OrgUnit, org_id: uuid.UUID) -> PortalUnitOut:
    """Public facility card."""
    return PortalUnitOut(id=str(u.id), org_id=str(org_id), name=u.name, address=u.address, phone=u.phone)


Place = tuple[PortalOrgOut, PortalUnitOut | None]


async def places_for(session: AsyncSession, unit_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Place]:
    """facility id → (its organisation card, its own card). The organisation is the nearest non-facility ancestor;
    deleted units still resolve (an order keeps naming its place)."""
    wanted = list(dict.fromkeys(u for u in unit_ids if u))
    if not wanted:
        return {}
    chains = {fid: await ancestor_ids(session, fid) for fid in wanted}
    units = await units_repo.units_by_ids(session, [*wanted, *(x for chain in chains.values() for x in chain)])
    out: dict[uuid.UUID, Place] = {}
    for fid in wanted:
        org = next((units[x] for x in reversed(chains[fid]) if x in units and units[x].level != "facility"), None)
        org_id = org.id if org is not None else fid
        facility = units.get(fid)
        out[fid] = (org_out(org, org_id), unit_out(facility, org_id) if facility is not None else None)
    return out


async def place_of(session: AsyncSession, o: Order) -> Place:
    """Organisation + facility cards of an order (headers and `{{org.*}}` / `{{unit.*}}` placeholders)."""
    return (await places_for(session, [o.unit_id]))[o.unit_id]


def document_pdf_url(document_id: uuid.UUID | str) -> str:
    """Portal PDF endpoint of a document."""
    return f"/api/v1/portal/documents/{document_id}/pdf"


def document_out(d: ResultDocument) -> ResultDocumentOut:
    """`ResultDocument` DTO with `pdfUrl` pointing at the portal PDF route."""
    return orders_svc.document_out(d).model_copy(update={"pdf_url": document_pdf_url(d.id)})


def item_out(i: OrderItem) -> OrderItemOut:
    """`OrderItem` DTO; results (`values`, `labNote`) only once approved; the staff-side trail never."""
    dto = orders_svc.item_out(i).model_copy(update={"history": []})
    if i.status == "approved":
        return dto
    return dto.model_copy(update={"values": {}, "lab_note": None})


# ----------------------------------------------------------------------------- identity


async def identity_ids(session: AsyncSession, principal: PatientPrincipal) -> list[uuid.UUID]:
    """All patient ids the principal may act for (same phone inside the same country); always includes itself.

    A session opened with a portal login is that one patient only: relatives sharing a phone each get their
    own login and must not see each other's results."""
    if principal.own_only:
        return [principal.id]
    ids = await repo.patient_ids_by_phone(session, principal.patient.phone, principal.patient.country_id)
    if principal.id not in ids:
        ids.append(principal.id)
    return ids


# ----------------------------------------------------------------------------- reads


async def overview(session: AsyncSession, principal: PatientPrincipal) -> PortalOverviewOut:
    """Patient card + all non-cancelled orders (every facility), their documents, the organisations and facilities."""
    ids = await identity_ids(session, principal)
    orders = await repo.orders_of_patients(session, ids)
    documents = await repo.documents_of_orders(session, [o.id for o in orders])
    places = await places_for(session, [o.unit_id for o in orders])
    orgs: dict[str, PortalOrgOut] = {}
    units: dict[str, PortalUnitOut] = {}
    for o in orders:
        org, unit = places[o.unit_id]
        orgs.setdefault(org.id, org)
        if unit is not None:
            units.setdefault(unit.id, unit)
    return PortalOverviewOut(
        patient=patient_out(principal.patient),
        orders=[orders_svc.order_out(o) for o in orders],
        documents=[document_out(d) for d in documents],
        orgs=list(orgs.values()),
        units=list(units.values()),
    )


async def get_owned_order(session: AsyncSession, order_id: uuid.UUID, principal: PatientPrincipal) -> Order:
    """Order owned by the principal's identity set or 404 'Chek topilmadi'."""
    o = await repo.get_owned_order(session, order_id, await identity_ids(session, principal))
    if not o:
        raise NotFoundError(ORDER_NOT_FOUND)
    return o


async def order(session: AsyncSession, order_id: uuid.UUID, principal: PatientPrincipal) -> PortalOrderOut:
    """Order + items (all statuses, results redacted until approved) + documents."""
    o = await get_owned_order(session, order_id, principal)
    items = await repo.items_of_order(session, o.id, o.unit_id)
    documents = await repo.documents_of_orders(session, [o.id])
    org, unit = await place_of(session, o)
    return PortalOrderOut(
        order=orders_svc.order_out(o),
        items=[item_out(i) for i in items],
        documents=[document_out(d) for d in documents],
        org=org,
        unit=unit,
    )


async def get_owned_document(
    session: AsyncSession, document_id: uuid.UUID, principal: PatientPrincipal
) -> tuple[ResultDocument, Order]:
    """Document (+ its order) owned by the principal's identity set or 404 'Hujjat topilmadi'."""
    found = await repo.get_owned_document(session, document_id, await identity_ids(session, principal))
    if not found:
        raise NotFoundError(DOC_NOT_FOUND)
    return found


async def document(session: AsyncSession, document_id: uuid.UUID, principal: PatientPrincipal) -> PortalDocumentOut:
    """Everything the portal viewer needs to render a result document."""
    d, o = await get_owned_document(session, document_id, principal)
    await orders_svc.mark_document_viewed(session, d)  # the patient opened the result
    template = await repo.get_template(session, d.template_id)
    if not template:
        raise NotFoundError(DOC_NOT_FOUND)
    covered_ids: list[uuid.UUID] = list(d.order_item_ids or ([d.order_item_id] if d.order_item_id else []))
    items = await repo.items_by_ids(session, covered_ids, d.unit_id)
    item = next((i for i in items if d.order_item_id and i.id == d.order_item_id), None)
    schemas = await repo.schemas_by_ids(session, [i.schema_id for i in items if i.schema_id])
    usage = await repo.schema_usage(session, [s.id for s in schemas])
    codes = await repo.service_codes(session, [i.service_type_id for i in items])
    service_codes = {str(i.service_type_id): codes.get(i.service_type_id) or str(i.service_type_id) for i in items}
    anchor = item or (items[0] if items else None)
    category = await repo.get_category(session, anchor.category_id) if anchor else None
    org, unit = await place_of(session, o)
    assets = await repo.assets_on_path(session, await ancestor_ids(session, d.unit_id))
    return PortalDocumentOut(
        document=document_out(d),
        template=templates_svc.template_out(template),
        item=item_out(item) if item else None,
        order=orders_svc.order_out(o),
        items=[item_out(i) for i in items],
        schemas=[catalog_svc.schema_out(s, usage.get(s.id, 0)) for s in schemas],
        service_codes=service_codes,
        category=catalog_svc.category_out(category) if category else None,
        org=org,
        unit=unit,
        assets=[templates_svc.asset_out(a) for a in assets],
    )


async def document_pdf(session: AsyncSession, document_id: uuid.UUID, principal: PatientPrincipal) -> tuple[bytes, str]:
    """PDF bytes + download filename of an owned document (rendered on demand when missing)."""
    d, _ = await get_owned_document(session, document_id, principal)
    pdf = await orders_svc.ensure_document_pdf(session, d)
    await orders_svc.mark_document_viewed(session, d)
    return pdf, orders_svc.pdf_filename(d)
