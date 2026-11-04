"""Patient portal login + password (besides phone + SMS code).

The clinic may issue a patient a portal login and password. A session opened with them sees that patient only
(family members sharing one phone each see their own results); phone + SMS code keeps working as before and still
sees every patient of the phone. `sessions.method` records how a patient signed in, so resetting a password ends
the login sessions without touching SMS-code ones. All columns are nullable / defaulted: existing rows are unchanged.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-11-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("patients", sa.Column("portal_login", sa.String(40), nullable=True))
    op.add_column("patients", sa.Column("portal_password_hash", sa.Text(), nullable=True))
    op.add_column("patients", sa.Column("portal_password_set_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("patients", sa.Column("portal_failed_logins", sa.SmallInteger(), nullable=False, server_default=sa.text("0")))
    op.add_column("patients", sa.Column("portal_locked_until", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "uq_patients_portal_login_alive",
        "patients",
        [sa.text("lower(portal_login)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND portal_login IS NOT NULL"),
    )
    op.add_column("sessions", sa.Column("method", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "method")
    op.drop_index("uq_patients_portal_login_alive", table_name="patients")
    op.drop_column("patients", "portal_locked_until")
    op.drop_column("patients", "portal_failed_logins")
    op.drop_column("patients", "portal_password_set_at")
    op.drop_column("patients", "portal_password_hash")
    op.drop_column("patients", "portal_login")
