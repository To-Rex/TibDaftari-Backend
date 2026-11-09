"""The organisation tree: Davlat → Viloyat → Tuman/Shahar → Muassasa(lar).

`org_units` is ONE table for every level. Geo columns are filled down to the level (a region node has
`region_id`, a facility has all three). The DB validates nesting (trigger `org_units_validate`) and keeps the
closure table (`org_unit_closure`) up to date; the app only inserts/updates rows and reads the closure.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, PKMixin, SoftDeleteMixin

UNIT_LEVELS: tuple[str, ...] = ("country", "region", "district", "facility")
FACILITY_KINDS: tuple[str, ...] = ("ses", "clinic", "laboratory", "polyclinic", "hospital", "pharmacy", "other")
#: the level a child of each level has
CHILD_LEVEL: dict[str, str | None] = {"country": "region", "region": "district", "district": "facility", "facility": None}
PARENT_LEVEL: dict[str, str | None] = {"country": None, "region": "country", "district": "region", "facility": "district"}


class OrgUnit(PKMixin, AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "org_units"

    level: Mapped[str] = mapped_column(String(10), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"))
    country_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("countries.id"), nullable=False)
    region_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("regions.id"))
    district_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("districts.id"))
    kind: Mapped[str | None] = mapped_column(String(20))  # facility only: ses | clinic | laboratory | …
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(80))
    code: Mapped[str | None] = mapped_column(String(12))  # facility: cheque prefix (UR-000123)
    legal_name: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(String(64))  # NULL → the country's (filled by the DB trigger)
    logo_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: inherited settings (documentFreeViewDays, locale, telegramWebhookSecret …); `effective_settings(unit)` merges them
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))

    @property
    def is_facility(self) -> bool:
        return self.level == "facility"


class OrgUnitClosure(Base):
    """(ancestor, descendant, depth) for every pair incl. self (depth 0). Maintained by DB triggers — read only."""

    __tablename__ = "org_unit_closure"

    ancestor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id", ondelete="CASCADE"), primary_key=True)
    descendant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id", ondelete="CASCADE"), primary_key=True)
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class OrderCounter(Base):
    """Cheque numbering per facility — bumped atomically by the SQL function `next_order_number(facility)`."""

    __tablename__ = "order_counters"

    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
