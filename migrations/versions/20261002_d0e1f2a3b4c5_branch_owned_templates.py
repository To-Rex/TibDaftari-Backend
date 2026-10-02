"""Every branch owns its own templates: a template shared by several branches (or bound to none) is split into one
independent copy per branch, so editing, activating or deleting it in one branch no longer changes the others.

The original row stays with the oldest of its branches (issued documents and service defaults keep pointing to
it); every other branch gets a copy with the same content, status and bindings, the original creation time (the
approval resolution order inside a branch stays the same) and usage 0. A company-wide template (no branch) is
copied to every branch of its company. Templates of companies without branches are left alone.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-10-02
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.core.ids import uuid7

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    bind = op.get_bind()
    # alive branches per company, oldest first
    branches: dict = {}
    for cid, bid in bind.execute(sa.text("SELECT company_id, id FROM branches WHERE deleted_at IS NULL ORDER BY created_at, id")).fetchall():
        branches.setdefault(cid, []).append(bid)
    rows = bind.execute(
        sa.text("SELECT id, company_id, branch_ids FROM result_templates WHERE deleted_at IS NULL AND cardinality(branch_ids) <> 1 ORDER BY created_at, id")
    ).fetchall()
    copy = sa.text(
        """
        INSERT INTO result_templates
            (id, company_id, name, description, status, version, service_type_ids, category_ids, branch_ids, scope,
             language, doc, thumbnail_url, usage, created_at, updated_at, created_by)
        SELECT :new_id, company_id, name, description, status, version, service_type_ids, category_ids, :branch_ids, scope,
               language, doc, thumbnail_url, 0, created_at, now(), created_by
        FROM result_templates WHERE id = :src
        """
    ).bindparams(sa.bindparam("new_id", type_=UUID), sa.bindparam("src", type_=UUID), sa.bindparam("branch_ids", type_=postgresql.ARRAY(UUID)))
    keep = sa.text("UPDATE result_templates SET branch_ids = :branch_ids, updated_at = now() WHERE id = :src").bindparams(
        sa.bindparam("src", type_=UUID), sa.bindparam("branch_ids", type_=postgresql.ARRAY(UUID))
    )
    for tid, cid, bound in rows:
        alive = branches.get(cid, [])
        # bound to several → those (alive) branches, oldest first; bound to none → every branch of the company
        targets = [b for b in alive if b in set(bound)] if bound else list(alive)
        if not targets:
            continue
        bind.execute(keep, {"src": tid, "branch_ids": [targets[0]]})
        for bid in targets[1:]:
            bind.execute(copy, {"new_id": uuid7(), "src": tid, "branch_ids": [bid]})


def downgrade() -> None:
    # the copies are independent templates now (possibly edited per branch) — nothing to merge back
    pass
