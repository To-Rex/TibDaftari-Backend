"""Patients get a home branch (the branch that registered them) so patient lists can be branch-scoped; older
patients take the branch of their first order. Outbox messages without a branch take their order's (else
their patient's) branch, so the outbox can be branch-scoped too.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-10-01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("patients", sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_patients_company_branch", "patients", ["company_id", "branch_id"])
    op.execute(
        sa.text(
            """
            UPDATE patients AS p
            SET branch_id = o.branch_id
            FROM (
                SELECT DISTINCT ON (patient_id) patient_id, branch_id
                FROM orders
                WHERE deleted_at IS NULL
                ORDER BY patient_id, created_at, id
            ) AS o
            WHERE o.patient_id = p.id AND p.branch_id IS NULL
            """
        )
    )
    op.execute(sa.text("UPDATE outbox_messages AS m SET branch_id = o.branch_id FROM orders AS o WHERE m.order_id = o.id AND m.branch_id IS NULL"))
    op.execute(sa.text("UPDATE outbox_messages AS m SET branch_id = p.branch_id FROM patients AS p WHERE m.patient_id = p.id AND m.branch_id IS NULL AND p.branch_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_patients_company_branch", table_name="patients")
    op.drop_column("patients", "branch_id")
