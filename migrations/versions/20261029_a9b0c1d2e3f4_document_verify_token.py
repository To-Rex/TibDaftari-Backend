"""QR authenticity check: result documents get their own verification token (`/v/{token}`).

The QR printed on a document no longer shares the SMS link's token — scanning it shows only whether the document is
authentic (never the PDF, never an SMS code). Existing documents keep NULL: their PDFs are frozen and their QR codes
already point at the SMS link (`/d/{public_token}`), which keeps working as before.

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-10-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a9b0c1d2e3f4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("result_documents", sa.Column("verify_token", sa.String(length=64), nullable=True))
    op.create_index("ix_result_documents_verify_token", "result_documents", ["verify_token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_result_documents_verify_token", table_name="result_documents")
    op.drop_column("result_documents", "verify_token")
