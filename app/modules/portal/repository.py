"""Portal SQL — every read is bounded by the set of patient ids the principal acts for (same phone, same country)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    AttributeSchema,
    Category,
    Order,
    OrderItem,
    ResultDocument,
    ResultTemplate,
    ServiceType,
    TemplateAsset,
)
from app.infrastructure.db.models import Patient as PatientModel


async def patient_ids_by_phone(session: AsyncSession, phone: str, country_id: uuid.UUID) -> list[uuid.UUID]:
    """Alive patients of the country registered under `phone` — the portal identity set."""
    stmt = select(PatientModel.id).where(PatientModel.phone == phone, PatientModel.country_id == country_id, alive(PatientModel))
    return [r[0] for r in await session.execute(stmt)]


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


async def assets_on_path(session: AsyncSession, unit_ids: Sequence[uuid.UUID]) -> list[TemplateAsset]:
    """Alive template assets (logo/stamp/signature/image) owned by the facility or the units above it — the images
    its documents may render."""
    if not unit_ids:
        return []
    stmt = select(TemplateAsset).where(TemplateAsset.owner_unit_id.in_(unit_ids), alive(TemplateAsset)).order_by(TemplateAsset.created_at.asc())
    return list((await session.execute(stmt)).scalars())


async def get_owned_order(session: AsyncSession, order_id: uuid.UUID, patient_ids: Sequence[uuid.UUID]) -> Order | None:
    """Alive order owned by the identity set (any status), else None — no existence leak."""
    if not patient_ids:
        return None
    stmt = select(Order).where(Order.id == order_id, Order.patient_id.in_(patient_ids), alive(Order))
    return (await session.execute(stmt)).scalar_one_or_none()


async def items_of_order(session: AsyncSession, order_id: uuid.UUID, unit_id: uuid.UUID) -> list[OrderItem]:
    """Alive items of an order (written at the order's facility) in creation order."""
    stmt = (
        select(OrderItem)
        .where(OrderItem.order_id == order_id, OrderItem.unit_id == unit_id, alive(OrderItem))
        .order_by(OrderItem.created_at.asc(), OrderItem.id.asc())
    )
    return list((await session.execute(stmt)).scalars())


async def items_by_ids(session: AsyncSession, ids: Sequence[uuid.UUID], unit_id: uuid.UUID) -> list[OrderItem]:
    """Alive items by id inside one facility, in creation order."""
    if not ids:
        return []
    stmt = (
        select(OrderItem)
        .where(OrderItem.id.in_(ids), OrderItem.unit_id == unit_id, alive(OrderItem))
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


async def get_template(session: AsyncSession, template_id: uuid.UUID) -> ResultTemplate | None:
    """Template referenced by a document (archived/deleted templates still resolve — history is immutable)."""
    return (await session.execute(select(ResultTemplate).where(ResultTemplate.id == template_id))).scalar_one_or_none()


async def schemas_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> list[AttributeSchema]:
    """Schemas referenced by items (any status), in stable id order."""
    wanted = list({i for i in ids if i})
    if not wanted:
        return []
    rows = (await session.execute(select(AttributeSchema).where(AttributeSchema.id.in_(wanted)))).scalars()
    return sorted(rows, key=lambda s: str(s.id))


async def schema_usage(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Count of alive service types per schema id (`usedBy`)."""
    if not ids:
        return {}
    stmt = select(ServiceType.schema_id, func.count()).where(ServiceType.schema_id.in_(ids), alive(ServiceType)).group_by(ServiceType.schema_id)
    return {r[0]: int(r[1]) for r in await session.execute(stmt)}


async def service_codes(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    """`service_type_id -> code` for the given ids (deleted service types still resolve)."""
    wanted = list({i for i in ids if i})
    if not wanted:
        return {}
    stmt = select(ServiceType.id, ServiceType.code).where(ServiceType.id.in_(wanted))
    return {r[0]: r[1] for r in await session.execute(stmt)}


async def get_category(session: AsyncSession, category_id: uuid.UUID) -> Category | None:
    """Category by id (deleted still resolves — referenced by history)."""
    return (await session.execute(select(Category).where(Category.id == category_id))).scalar_one_or_none()
