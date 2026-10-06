"""Result documents record whether the patient got them: first / last open by the patient (public link or portal)
with a counter, and first print by staff with a counter. Feeds the "results received" reports; nothing is
backfilled (opens and prints are known from this release on).

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("result_documents", sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("result_documents", sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("result_documents", sa.Column("view_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("result_documents", sa.Column("printed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("result_documents", sa.Column("print_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.create_index("ix_result_documents_order", "result_documents", ["company_id", "order_id"])


def downgrade() -> None:
    op.drop_index("ix_result_documents_order", table_name="result_documents")
    for col in ("print_count", "printed_at", "view_count", "last_viewed_at", "viewed_at"):
        op.drop_column("result_documents", col)
