"""The registrar desk sees approved results: every company's `registrator` role gets `confirm.result.view` (approved
results only — view and print) and `confirm.result.resend` (re-send the result SMS). Other roles are untouched;
admins can still change any role in the roles matrix.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-10-09
"""
from __future__ import annotations

from alembic import op

revision = "a3b4c5d6e7f8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None

UPGRADE = (
    "UPDATE roles SET permissions = array_append(permissions, 'confirm.result.view'::varchar) "
    "WHERE key = 'registrator' AND deleted_at IS NULL AND NOT ('confirm.result.view' = ANY(permissions))",
    "UPDATE roles SET permissions = array_append(permissions, 'confirm.result.resend'::varchar) "
    "WHERE key = 'registrator' AND deleted_at IS NULL AND NOT ('confirm.result.resend' = ANY(permissions))",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    # resend may have been granted by hand before — only the new permission is taken back
    op.execute("UPDATE roles SET permissions = array_remove(permissions, 'confirm.result.view'::varchar) WHERE key = 'registrator'")
