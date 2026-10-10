"""Every branch may send SMS through its own Xabarchi key. Until a branch saves one (`sms_provider` NULL) it keeps
using the company's key, so nothing changes for existing branches.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-10-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c5d6e7f8a9b0"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("branches", sa.Column("sms_provider", sa.String(length=20), nullable=True))
    op.add_column("branches", sa.Column("sms_api_key_enc", sa.Text(), nullable=True))
    op.add_column("branches", sa.Column("sms_api_key_masked", sa.String(length=80), nullable=True))
    op.add_column("branches", sa.Column("sms_default_priority", sa.String(length=20), nullable=True))
    op.add_column("branches", sa.Column("sms_sender_note", sa.String(length=200), nullable=True))


def downgrade() -> None:
    for col in ("sms_sender_note", "sms_default_priority", "sms_api_key_masked", "sms_api_key_enc", "sms_provider"):
        op.drop_column("branches", col)
