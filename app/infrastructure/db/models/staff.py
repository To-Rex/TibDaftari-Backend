"""Staff tables: roles, employees (+ extra scope units), sessions, OTP challenges.

Scope: an employee belongs to one home unit (`unit_id`, any level) and may get extra units
(`employee_units`); their effective scope is the union of those subtrees. A superadmin has no unit.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, PKMixin, SoftDeleteMixin


class Role(PKMixin, AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "roles"

    #: NULL = system role (available everywhere); else usable inside that unit's subtree only
    scope_unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), nullable=True, index=True)
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Employee(PKMixin, AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "employees"

    #: home unit (any level); NULL only for a superadmin
    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), nullable=True, index=True)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    login: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
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
        Index("ix_employees_unit_status", "unit_id", "status"),
    )


class EmployeeUnit(Base):
    """Extra scope units of an employee (besides the home unit)."""

    __tablename__ = "employee_units"

    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True)
    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), primary_key=True)


class Session(Base):
    """Issued access tokens (jti) — allow-list for instant revocation + audit trail."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # jti
    actor: Mapped[str] = mapped_column(String(10), nullable=False)  # staff | patient
    # patients: otp (phone + SMS code) | login (portal login + password); staff: password
    method: Mapped[str | None] = mapped_column(String(10))
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OtpChallenge(PKMixin, Base):
    __tablename__ = "otp_challenges"

    phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)  # portal | telegram | document
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
