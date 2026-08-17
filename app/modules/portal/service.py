"""Patient portal — read-only self-service surface (DOMAIN_RULES section 11).

Identity: a patient may be registered in several clinics under the same phone; the portal
principal therefore owns every alive patient row sharing its phone (computed once per request).
Ownership checks answer 404 (never 403) so foreign ids do not leak existence.
Item results are redacted: `values`/`labNote` are exposed only for approved items.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PatientPrincipal
from app.core.exceptions import NotFoundError
from app.infrastructure.db.models import Branch, Company, Order, OrderItem, ResultDocument
from app.infrastructure.db.models import Patient as PatientModel
from app.modules.catalog import service as catalog_svc
from app.modules.orders import service as orders_svc
from app.modules.orders.schemas import OrderItemOut, ResultDocumentOut
from app.modules.portal import repository as repo
from app.modules.portal.schemas import (
    PortalAddressOut,
    PortalBranchOut,
    PortalCompanyOut,
    PortalDocumentOut,
    PortalLinkOut,
    PortalOrderOut,
    PortalOverviewOut,
    PortalPatientOut,
    PortalStatsOut,
)
from app.modules.templates import service as templates_svc

ORDER_NOT_FOUND = "Chek topilmadi"
DOC_NOT_FOUND = "Hujjat topilmadi"


# ----------------------------------------------------------------------------- projections


def patient_out(p: PatientModel) -> PortalPatientOut:
    """Safe patient projection: identity documents, note, discount and tags are neutralised."""
    return PortalPatientOut(
        id=str(p.id),
        company_id=str(p.company_id),
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


def company_out(company: Company | None, company_id: uuid.UUID) -> PortalCompanyOut:
    """Public clinic card; a missing company (hard-deleted) degrades to an id-only card."""
    if not company:
        return PortalCompanyOut(id=str(company_id), name="")
    return PortalCompanyOut(id=str(company.id), name=company.name, logo_url=company.logo_url, phone=company.phone, address=company.address)


def branch_out(b: Branch) -> PortalBranchOut:
    """Public branch card."""
    return PortalBranchOut(id=str(b.id), company_id=str(b.company_id), name=b.name, address=b.address, phone=b.phone)


async def place_of(session: AsyncSession, o: Order) -> tuple[PortalCompanyOut, PortalBranchOut | None]:
    """Clinic + branch cards of an order (headers and `{{company.*}}` / `{{branch.*}}` placeholders)."""
    companies = await repo.companies_by_ids(session, [o.company_id])
    branch = (await repo.branches_by_ids(session, [o.branch_id])).get(o.branch_id)
    return company_out(companies.get(o.company_id), o.company_id), (branch_out(branch) if branch else None)


def document_pdf_url(document_id: uuid.UUID | str) -> str:
    """Portal PDF endpoint of a document."""
    return f"/api/v1/portal/documents/{document_id}/pdf"


def document_out(d: ResultDocument) -> ResultDocumentOut:
    """`ResultDocument` DTO with `pdfUrl` pointing at the portal PDF route."""
    return orders_svc.document_out(d).model_copy(update={"pdf_url": document_pdf_url(d.id)})


def item_out(i: OrderItem) -> OrderItemOut:
    """`OrderItem` DTO; results (`values`, `labNote`) only once approved."""
    dto = orders_svc.item_out(i)
    if i.status == "approved":
        return dto
    return dto.model_copy(update={"values": {}, "lab_note": None})


# ----------------------------------------------------------------------------- identity


async def identity_ids(session: AsyncSession, principal: PatientPrincipal) -> list[uuid.UUID]:
    """All patient ids the principal may act for (same phone across clinics); always includes itself."""
    ids = await repo.patient_ids_by_phone(session, principal.patient.phone)
    if principal.id not in ids:
        ids.append(principal.id)
    return ids


# ----------------------------------------------------------------------------- reads


async def overview(session: AsyncSession, principal: PatientPrincipal) -> PortalOverviewOut:
    """Patient card + all non-cancelled orders (every clinic), their documents and the clinics list."""
    ids = await identity_ids(session, principal)
    orders = await repo.orders_of_patients(session, ids)
    documents = await repo.documents_of_orders(session, [o.id for o in orders])
    company_ids: list[uuid.UUID] = []
    for o in orders:
        if o.company_id not in company_ids:
            company_ids.append(o.company_id)
    companies = await repo.companies_by_ids(session, company_ids)
    branches = await repo.branches_by_ids(session, [o.branch_id for o in orders])
    return PortalOverviewOut(
        patient=patient_out(principal.patient),
        orders=[orders_svc.order_out(o) for o in orders],
        documents=[document_out(d) for d in documents],
        companies=[company_out(companies.get(cid), cid) for cid in company_ids],
        branches=[branch_out(b) for b in branches.values()],
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
    items = await repo.items_of_order(session, o.id, o.company_id)
    documents = await repo.documents_of_orders(session, [o.id])
    company, branch = await place_of(session, o)
    return PortalOrderOut(
        order=orders_svc.order_out(o),
        items=[item_out(i) for i in items],
        documents=[document_out(d) for d in documents],
        company=company,
        branch=branch,
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
    template = await repo.get_template(session, d.template_id, d.company_id)
    if not template:
        raise NotFoundError(DOC_NOT_FOUND)
    covered_ids: list[uuid.UUID] = list(d.order_item_ids or ([d.order_item_id] if d.order_item_id else []))
    items = await repo.items_by_ids(session, covered_ids, d.company_id)
    item = next((i for i in items if d.order_item_id and i.id == d.order_item_id), None)
    schemas = await repo.schemas_by_ids(session, [i.schema_id for i in items if i.schema_id], d.company_id)
    usage = await repo.schema_usage(session, [s.id for s in schemas], d.company_id)
    codes = await repo.service_codes(session, [i.service_type_id for i in items], d.company_id)
    service_codes = {str(i.service_type_id): codes.get(i.service_type_id) or str(i.service_type_id) for i in items}
    anchor = item or (items[0] if items else None)
    category = await repo.get_category(session, anchor.category_id, d.company_id) if anchor else None
    company, branch = await place_of(session, o)
    assets = await repo.assets_of_company(session, d.company_id)
    return PortalDocumentOut(
        document=document_out(d),
        template=templates_svc.template_out(template),
        item=item_out(item) if item else None,
        order=orders_svc.order_out(o),
        items=[item_out(i) for i in items],
        schemas=[catalog_svc.schema_out(s, usage.get(s.id, 0)) for s in schemas],
        service_codes=service_codes,
        category=catalog_svc.category_out(category) if category else None,
        company=company,
        branch=branch,
        assets=[templates_svc.asset_out(a) for a in assets],
    )


async def document_pdf(session: AsyncSession, document_id: uuid.UUID, principal: PatientPrincipal) -> tuple[bytes, str]:
    """PDF bytes + download filename of an owned document (rendered on demand when missing)."""
    d, _ = await get_owned_document(session, document_id, principal)
    pdf = await orders_svc.ensure_document_pdf(session, d)
    return pdf, orders_svc.pdf_filename(d)
