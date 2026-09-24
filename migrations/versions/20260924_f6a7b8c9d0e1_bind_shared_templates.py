"""Templates belong to branches: every company-wide template (no branch binding) is bound to all active
branches of its company, so each branch's gallery keeps showing it and results keep rendering. Branches
then own their templates — copies/imports are branch-local.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE result_templates AS rt
            SET branch_ids = sub.ids
            FROM (
                SELECT company_id, array_agg(id ORDER BY created_at, id) AS ids
                FROM branches
                WHERE deleted_at IS NULL AND is_active
                GROUP BY company_id
            ) AS sub
            WHERE rt.company_id = sub.company_id
              AND rt.deleted_at IS NULL
              AND cardinality(rt.branch_ids) = 0
            """
        )
    )


def downgrade() -> None:
    # which templates used to be company-wide is not recorded — bindings stay as they are
    pass
