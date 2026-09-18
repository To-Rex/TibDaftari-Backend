"""branches.country_id / region_id / district_id — branch location (same reference tables as companies)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col, ref in (("country_id", "countries"), ("region_id", "regions"), ("district_id", "districts")):
        op.add_column("branches", sa.Column(col, postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(f"fk_branches_{col}", "branches", ref, [col], ["id"])


def downgrade() -> None:
    for col in ("district_id", "region_id", "country_id"):
        op.drop_constraint(f"fk_branches_{col}", "branches", type_="foreignkey")
        op.drop_column("branches", col)
