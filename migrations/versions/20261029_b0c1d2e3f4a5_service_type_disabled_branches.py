"""Per-branch availability of catalog services: `service_types.disabled_branch_ids`.

A service does not have to exist in every branch of a company. The admin switches it off per branch in
"Xizmatni tahrirlash"; that branch no longer sees the service at reception and the API refuses to add it to the
branch's cheques (409 not_in_branch). Empty array = offered in every branch, so existing rows keep today's behaviour.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-10-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_types",
        sa.Column("disabled_branch_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'::uuid[]")),
    )


def downgrade() -> None:
    op.drop_column("service_types", "disabled_branch_ids")
