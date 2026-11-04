"""Payment method corrections: `payments.corrections` (JSONB list, oldest first) records what a payment was before its
method was corrected or it was split over several methods. Older rows get an empty list.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-11-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f4a5b6c7d8e9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("corrections", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.drop_column("payments", "corrections")
