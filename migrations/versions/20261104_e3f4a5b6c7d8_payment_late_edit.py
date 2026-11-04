"""Payment edit window: reception changes a payment (refund, money back on a removed service, delete the cheque) only
within a day of taking it; later it needs `reception.payment.late_edit`. Every company's `admin` and `rahbar` roles
get it; other roles are untouched (admins can still grant it to any role in the roles matrix). Superadmin always can.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-11-04
"""
from __future__ import annotations

from alembic import op

revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE roles SET permissions = array_append(permissions, 'reception.payment.late_edit'::varchar) "
        "WHERE key IN ('admin', 'rahbar') AND deleted_at IS NULL AND NOT ('reception.payment.late_edit' = ANY(permissions))"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE roles SET permissions = array_remove(permissions, 'reception.payment.late_edit'::varchar) WHERE key IN ('admin', 'rahbar')"
    )
