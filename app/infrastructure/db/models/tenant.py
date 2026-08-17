"""Tenant tables: companies, branches, roles, employees, sessions, OTP challenges."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, PKMixin, SoftDeleteMixin, TenantMixin


class Company(PKMixin, AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(300))
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    logo_url: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str] = mapped_column(String(2), nullable=False, default="uz")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # SMS provider (per company — each clinic owns its Xabarchi account)
    sms_provider: Mapped[str] = mapped_column(String(20), nullable=False, default="none")  # none | xabarchi
    sms_api_key_enc: Mapped[str | None] = mapped_column(Text)  # Fernet-encrypted plaintext key
    sms_api_key_masked: Mapped[str | None] = mapped_column(String(80))
    sms_default_priority: Mapped[str] = mapped_column(String(20), nullable=False, default="transactional")
    sms_sender_note: Mapped[str | None] = mapped_column(String(200))

    # Telegram bot (per company)
    telegram_bot_token_enc: Mapped[str | None] = mapped_column(Text)
    telegram_bot_username: Mapped[str | None] = mapped_column(String(80))
    telegram_bot_token_masked: Mapped[str | None] = mapped_column(String(80))

    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))

    __table_args__ = (Index("uq_companies_slug_alive", "slug", unique=True, postgresql_where=text("deleted_at IS NULL")),)


class Branch(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "branches"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(12), nullable=False)  # used in cheque numbers e.g. UR-000123
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(40))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Tashkent")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    order_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("uq_branches_company_code_alive", "company_id", "code", unique=True, postgresql_where=text("deleted_at IS NULL")),
    )


class Role(PKMixin, AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "roles"

    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)  # NULL = platform built-in
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(String(80)), nullable=False, default=list)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("uq_roles_company_key_alive", "company_id", "key", unique=True, postgresql_where=text("deleted_at IS NULL")),
    )


class Employee(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "employees"

    branch_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    login: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False, index=True)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=lambda: {"allow": [], "deny": []})
    category_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")  # active | inactive
    avatar_hue: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=160)
    is_super_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_logins: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signature_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        # login is global (single staff login form on the landing page)
        Index("uq_employees_login_alive", text("lower(login)"), unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_employees_company_status", "company_id", "status"),
    )


class Session(Base):
    """Issued access tokens (jti) — allow-list for instant revocation + audit trail."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # jti
    actor: Mapped[str] = mapped_column(String(10), nullable=False)  # staff | patient
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OtpChallenge(PKMixin, Base):
    __tablename__ = "otp_challenges"

    phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)  # portal | telegram
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
