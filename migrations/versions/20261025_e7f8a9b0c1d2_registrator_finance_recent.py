"""Registrator finance: grant `reports.finance.recent` (last-7-days revenue) to existing registrar roles.

New companies already get it from DEFAULT_COMPANY_ROLES; this migration updates the roles
created before the permission existed.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-10-25
"""
from __future__ import annotations

from alembic import op

revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE roles SET permissions = permissions || ARRAY['reports.finance.recent'] "
        "WHERE key = 'registrator' AND NOT ('reports.finance.recent' = ANY(permissions))"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE roles SET permissions = array_remove(permissions, 'reports.finance.recent') "
        "WHERE key = 'registrator'"
    )
