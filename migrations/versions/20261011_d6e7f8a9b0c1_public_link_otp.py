"""Public result links: first open is direct, later opens require an SMS code.

Adds the public-link access counters; existing documents start at zero (their next
public open is treated as the first one). The ordinary `view_count` stays untouched.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-10-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("result_documents", sa.Column("public_view_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("result_documents", sa.Column("public_first_viewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("result_documents", sa.Column("public_last_viewed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ("public_last_viewed_at", "public_first_viewed_at", "public_view_count"):
        op.drop_column("result_documents", col)
