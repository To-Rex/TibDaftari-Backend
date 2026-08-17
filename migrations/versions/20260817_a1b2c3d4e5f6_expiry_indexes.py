"""expiry indexes for session / otp pruning

Revision ID: a1b2c3d4e5f6
Revises: 8d88fac9b4e6
Create Date: 2026-08-17 12:00:00
"""
from __future__ import annotations

from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "8d88fac9b4e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)
    op.create_index("ix_otp_challenges_expires_at", "otp_challenges", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_otp_challenges_expires_at", table_name="otp_challenges")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
