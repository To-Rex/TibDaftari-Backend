"""Portal DTOs — the patient self-service wire shapes (Clinic-Web `PortalRepository`).

Order/OrderItem/ResultDocument/ResultTemplate/AttributeSchema/Category DTOs are the same classes
the staff surface uses (`orders`, `templates`, `catalog` schemas); only the patient projection is
portal-specific because sensitive fields are neutralised.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from app.core.schemas import CamelModel
from app.modules.catalog.schemas import AttributeSchemaOut, CategoryOut
from app.modules.orders.schemas import OrderItemOut, OrderOut, ResultDocumentOut
from app.modules.templates.schemas import TemplateAssetOut, TemplateOut


class PortalAddressOut(CamelModel):
    region_id: str | None = None
    district_id: str | None = None
    street: str | None = None


class PortalStatsOut(CamelModel):
    orders: int
    last_visit_at: datetime | None = None
    total_spent: int


class PortalLinkOut(CamelModel):
    linked: bool
    telegram_chat_id: str | None = None


class PortalPatientOut(CamelModel):
    """Frontend `Patient` minus sensitive data: no note/pinfl/passport/contract/workplace,
    `discountPercent` fixed to 0 and `tags` empty."""

    id: str
    company_id: str
    full_name: str
    phone: str
    gender: Literal["male", "female"] | None = None
    birth_date: date | None = None
    address: PortalAddressOut
    discount_percent: int = 0
    tags: list[str]
    stats: PortalStatsOut
    portal: PortalLinkOut
    created_at: datetime
    updated_at: datetime


class PortalCompanyOut(CamelModel):
    """Public clinic card (letterhead data) — no settings, secrets or counters."""

    id: str
    name: str
    logo_url: str | None = None
    phone: str | None = None
    address: str | None = None


class PortalBranchOut(CamelModel):
    """Public branch card — enough for `{{branch.*}}` placeholders and place labels."""

    id: str
    company_id: str
    name: str
    address: str | None = None
    phone: str | None = None


class PortalOverviewOut(CamelModel):
    patient: PortalPatientOut
    orders: list[OrderOut]
    documents: list[ResultDocumentOut]
    companies: list[PortalCompanyOut]
    branches: list[PortalBranchOut]


class PortalOrderOut(CamelModel):
    order: OrderOut
    items: list[OrderItemOut]
    documents: list[ResultDocumentOut]
    company: PortalCompanyOut
    branch: PortalBranchOut | None = None


class PortalDocumentOut(CamelModel):
    document: ResultDocumentOut
    template: TemplateOut
    item: OrderItemOut | None = None
    order: OrderOut
    items: list[OrderItemOut]
    schemas: list[AttributeSchemaOut]
    service_codes: dict[str, str]
    category: CategoryOut | None = None
    company: PortalCompanyOut
    branch: PortalBranchOut | None = None
    assets: list[TemplateAssetOut]
