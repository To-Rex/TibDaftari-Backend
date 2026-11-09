"""Patients (one registry per country) + Telegram links."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, PKMixin, SoftDeleteMixin


class Patient(PKMixin, AuditMixin, SoftDeleteMixin, Base):
    """Identity rules (per country): passport & PINFL unique; else phone unique.
    Enforced by partial unique indexes + service-level duplicate checks."""

    __tablename__ = "patients"

    country_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("countries.id"), nullable=False)
    #: the facility that registered the patient — lists are scoped by it, identity is country-wide
    registered_unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"))
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)  # normalized 998XXXXXXXXX
    phone_extra: Mapped[str | None] = mapped_column(String(20))
    gender: Mapped[str | None] = mapped_column(String(6))  # male | female
    birth_date: Mapped[date | None] = mapped_column(Date)
    passport_number: Mapped[str | None] = mapped_column(String(20))
    pinfl: Mapped[str | None] = mapped_column(String(14))
    region_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("regions.id"))
    district_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("districts.id"))
    street: Mapped[str | None] = mapped_column(String(300))
    workplace: Mapped[str | None] = mapped_column(String(200))
    discount_percent: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    contract_number: Mapped[str | None] = mapped_column(String(60))
    note: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    # denormalised stats (maintained by the orders service)
    stats_orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stats_last_visit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats_total_spent: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    portal_linked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32))
    portal_last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # portal login + password issued at reception (optional, besides phone + SMS code); a session opened
    # with them sees this patient only. Login is global (one portal form).
    portal_login: Mapped[str | None] = mapped_column(String(40))
    portal_password_hash: Mapped[str | None] = mapped_column(Text)
    portal_password_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    portal_failed_logins: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default=text("0"))
    portal_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_patients_registered_unit", "registered_unit_id", "created_at"),
        Index("ix_patients_country_phone", "country_id", "phone"),
        Index("ix_patients_country_created", "country_id", "created_at"),
        Index("ix_patients_telegram_chat", "telegram_chat_id"),
    )


class TelegramLink(PKMixin, Base):
    """Patient ↔ Telegram chat link history (never deleted; unlink = unlinked_at). `account_id` = the bot."""

    __tablename__ = "telegram_links"

    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messaging_accounts.id"))
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    lang: Mapped[str] = mapped_column(String(4), nullable=False, default="uz")
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    unlinked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramChatPref(Base):
    """Per-chat preferences (language) of one bot — kept even when not linked."""

    __tablename__ = "telegram_chat_prefs"

    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("messaging_accounts.id"), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    lang: Mapped[str] = mapped_column(String(4), nullable=False, default="uz")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
