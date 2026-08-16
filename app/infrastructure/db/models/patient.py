"""Patients + geo reference (regions/districts) + Telegram links."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, PKMixin, SoftDeleteMixin, TenantMixin


class Region(PKMixin, Base):
    __tablename__ = "regions"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str | None] = mapped_column(String(20))
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class District(PKMixin, Base):
    __tablename__ = "districts"

    region_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("regions.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str | None] = mapped_column(String(20))
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Patient(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    """Identity rules: passport & PINFL unique per company; else phone unique.
    Enforced by partial unique indexes below + service-level duplicate checks."""

    __tablename__ = "patients"

    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)  # normalized 998XXXXXXXXX
    phone_extra: Mapped[str | None] = mapped_column(String(20))
    gender: Mapped[str | None] = mapped_column(String(6))  # male | female
    birth_date: Mapped[date | None] = mapped_column(Date)
    passport_number: Mapped[str | None] = mapped_column(String(20))
    pinfl: Mapped[str | None] = mapped_column(String(14))
    region_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    district_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    street: Mapped[str | None] = mapped_column(String(300))
    workplace: Mapped[str | None] = mapped_column(String(200))
    discount_percent: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    contract_number: Mapped[str | None] = mapped_column(String(60))
    note: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), nullable=False, default=list)
    # denormalised stats (maintained by the orders service)
    stats_orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stats_last_visit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats_total_spent: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    portal_linked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32))
    portal_last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_patients_company_passport_alive",
            "company_id",
            "passport_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND passport_number IS NOT NULL"),
        ),
        Index(
            "uq_patients_company_pinfl_alive",
            "company_id",
            "pinfl",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND pinfl IS NOT NULL"),
        ),
        Index(
            "uq_patients_company_phone_noid_alive",
            "company_id",
            "phone",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND passport_number IS NULL AND pinfl IS NULL"),
        ),
        Index("ix_patients_company_phone", "company_id", "phone"),
        Index("ix_patients_company_created", "company_id", "created_at"),
        Index("ix_patients_telegram_chat", "telegram_chat_id"),
        # trigram search on name/phone (created in migration: GIN gin_trgm_ops)
    )


class TelegramLink(PKMixin, Base):
    """Patient ↔ Telegram chat link history (never deleted; unlink = unlinked_at)."""

    __tablename__ = "telegram_links"

    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    lang: Mapped[str] = mapped_column(String(4), nullable=False, default="uz")
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    unlinked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramChatPref(Base):
    """Per-chat preferences (language) — kept even when not linked."""

    __tablename__ = "telegram_chat_prefs"

    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    lang: Mapped[str] = mapped_column(String(4), nullable=False, default="uz")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
