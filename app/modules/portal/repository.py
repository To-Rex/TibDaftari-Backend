"""Portal SQL — every read is bounded by the set of patient ids that share the principal's phone."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    AttributeSchema,
    Category,
    Company,
    Order,
    OrderItem,
    ResultDocument,
    ResultTemplate,
    ServiceType,
)
from app.infrastructure.db.models import Patient as PatientModel


async def patient_ids_by_phone(session: AsyncSession, phone: str) -> list[uuid.UUID]:
    """Alive patients (any clinic) registered under `phone` — the portal identity set."""
    rows = await session.execute(select(PatientModel.id).where(PatientModel.phone == phone, alive(PatientModel)))
    return [r[0] for r in rows]


async def orders_of_patients(session: AsyncSession, patient_ids: Sequence[uuid.UUID]) -> list[Order]:
    """Non-cancelled orders of the identity set, newest first (uses ix_orders_patient_created)."""
    if not patient_ids:
        return []
    stmt = (
        select(Order)
        .where(Order.patient_id.in_(patient_ids), Order.status != "cancelled", alive(Order))
        .order_by(Order.created_at.desc(), Order.id.desc())
    )
    return list((await session.execute(stmt)).scalars())


async def documents_of_orders(session: AsyncSession, order_ids: Sequence[uuid.UUID]) -> list[ResultDocument]:
    """Result documents of the given orders, newest first."""
    if not order_ids:
        return []
    stmt = (
        select(ResultDocument)
        .where(ResultDocument.order_id.in_(order_ids), alive(ResultDocument))
        .order_by(ResultDocument.created_at.desc(), ResultDocument.id.desc())
    )
    return list((await session.execute(stmt)).scalars())


async def companies_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Company]:
    """Companies by id (deleted ones included: an order keeps naming its clinic)."""
    wanted = list({i for i in ids if i})
    if not wanted:
        return {}
    rows = await session.execute(select(Company).where(Company.id.in_(wanted)))
    return {c.id: c for c in rows.scalars()}


async def get_owned_order(session: AsyncSession, order_id: uuid.UUID, patient_ids: Sequence[uuid.UUID]) -> Order | None:
    """Alive order owned by the identity set (any status), else None — no existence leak."""
    if not patient_ids:
        return None
    stmt = select(Order).where(Order.id == order_id, Order.patient_id.in_(patient_ids), alive(Order))
    return (await session.execute(stmt)).scalar_one_or_none()


async def items_of_order(session: AsyncSession, order_id: uuid.UUID, company_id: uuid.UUID) -> list[OrderItem]:
    """Alive items of an order in creation order."""
    stmt = (
        select(OrderItem)
        .where(OrderItem.order_id == order_id, OrderItem.company_id == company_id, alive(OrderItem))
        .order_by(OrderItem.created_at.asc(), OrderItem.id.asc())
    )
    return list((await session.execute(stmt)).scalars())


async def items_by_ids(session: AsyncSession, ids: Sequence[uuid.UUID], company_id: uuid.UUID) -> list[OrderItem]:
    """Alive items by id inside one company, in creation order."""
    if not ids:
        return []
    stmt = (
        select(OrderItem)
        .where(OrderItem.id.in_(ids), OrderItem.company_id == company_id, alive(OrderItem))
        .order_by(OrderItem.created_at.asc(), OrderItem.id.asc())
    )
    return list((await session.execute(stmt)).scalars())


async def get_owned_document(
    session: AsyncSession, document_id: uuid.UUID, patient_ids: Sequence[uuid.UUID]
) -> tuple[ResultDocument, Order] | None:
    """Alive document + its order when the order belongs to the identity set, else None."""
    if not patient_ids:
        return None
    stmt = (
        select(ResultDocument, Order)
        .join(Order, Order.id == ResultDocument.order_id)
        .where(
            ResultDocument.id == document_id,
            alive(ResultDocument),
            alive(Order),
            Order.patient_id.in_(patient_ids),
        )
    )
    row = (await session.execute(stmt)).first()
    return (row[0], row[1]) if row else None


async def get_template(session: AsyncSession, template_id: uuid.UUID, company_id: uuid.UUID) -> ResultTemplate | None:
    """Template referenced by a document (archived/deleted templates still resolve — history is immutable)."""
    stmt = select(ResultTemplate).where(ResultTemplate.id == template_id, ResultTemplate.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def schemas_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID], company_id: uuid.UUID) -> list[AttributeSchema]:
    """Schemas referenced by items (any status), in stable id order."""
    wanted = list({i for i in ids if i})
    if not wanted:
        return []
    stmt = select(AttributeSchema).where(AttributeSchema.id.in_(wanted), AttributeSchema.company_id == company_id)
    return sorted((await session.execute(stmt)).scalars(), key=lambda s: str(s.id))


async def schema_usage(session: AsyncSession, ids: Sequence[uuid.UUID], company_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """Count of alive service types per schema id (`usedBy`)."""
    if not ids:
        return {}
    stmt = (
        select(ServiceType.schema_id, func.count())
        .where(ServiceType.schema_id.in_(ids), ServiceType.company_id == company_id, alive(ServiceType))
        .group_by(ServiceType.schema_id)
    )
    return {r[0]: int(r[1]) for r in await session.execute(stmt)}


async def service_codes(session: AsyncSession, ids: Iterable[uuid.UUID], company_id: uuid.UUID) -> dict[uuid.UUID, str | None]:
    """`service_type_id -> code` for the given ids (deleted service types still resolve)."""
    wanted = list({i for i in ids if i})
    if not wanted:
        return {}
    stmt = select(ServiceType.id, ServiceType.code).where(ServiceType.id.in_(wanted), ServiceType.company_id == company_id)
    return {r[0]: r[1] for r in await session.execute(stmt)}


async def get_category(session: AsyncSession, category_id: uuid.UUID, company_id: uuid.UUID) -> Category | None:
    """Category by id inside the company (deleted still resolves — referenced by history)."""
    stmt = select(Category).where(Category.id == category_id, Category.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()
