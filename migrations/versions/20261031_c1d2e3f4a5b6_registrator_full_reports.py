"""Registrator reports like the admin's: grant `reports.finance.read` (revenue without the 7-day cap) and
`reports.export` (CSV / Excel) to existing registrar roles.

Only registrar roles that still see reports (`reports.operations.read` or `reports.finance.recent`) are touched —
a company that took reports away from its registrars keeps that choice. New companies get both permissions from
DEFAULT_COMPANY_ROLES. Reports stay confined to the employee's branches (`branch_scope`), as before.

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-10-31
"""
from __future__ import annotations

from alembic import op

revision = "c1d2e3f4a5b6"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None

def upgrade() -> None:
    # only registrar roles that still see reports (operations.read or finance.recent)
    op.execute(
        "UPDATE roles SET permissions = permissions || ARRAY['reports.finance.read'] "
        "WHERE key = 'registrator' AND NOT ('reports.finance.read' = ANY(permissions)) "
        "AND ('reports.operations.read' = ANY(permissions) OR 'reports.finance.recent' = ANY(permissions))"
    )
    op.execute(
        "UPDATE roles SET permissions = permissions || ARRAY['reports.export'] "
        "WHERE key = 'registrator' AND NOT ('reports.export' = ANY(permissions)) "
        "AND ('reports.operations.read' = ANY(permissions) OR 'reports.finance.recent' = ANY(permissions))"
    )


def downgrade() -> None:
    op.execute("UPDATE roles SET permissions = array_remove(permissions, 'reports.finance.read') WHERE key = 'registrator'")
    op.execute("UPDATE roles SET permissions = array_remove(permissions, 'reports.export') WHERE key = 'registrator'")
