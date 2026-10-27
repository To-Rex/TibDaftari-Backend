"""Cheque deletion: every company's `registrator` role gets `reception.order.delete` (delete a cheque that never
"happened" — unpaid, no lab work). Other roles are untouched; admins can still change any role in the roles matrix.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-10-27
"""
from __future__ import annotations

from alembic import op

revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE roles SET permissions = array_append(permissions, 'reception.order.delete'::varchar) "
        "WHERE key = 'registrator' AND deleted_at IS NULL AND NOT ('reception.order.delete' = ANY(permissions))"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE roles SET permissions = array_remove(permissions, 'reception.order.delete'::varchar) WHERE key = 'registrator'"
    )
