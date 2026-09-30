"""The first run of a7b8c9d0e1f2 stored the seeded cheque's `doc` as a JSON *string* (double-encoded); such rows
break every template list. Unwrap them back into objects.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE result_templates SET doc = (doc #>> '{}')::jsonb WHERE jsonb_typeof(doc) = 'string'"))


def downgrade() -> None:
    pass
