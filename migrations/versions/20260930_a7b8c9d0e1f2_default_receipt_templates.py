"""Every company with a branch gets the standard cheque ("Standart chek") as a draft receipt template bound to
all its active branches — unless it already has a receipt template. New companies get it with their first
branch (see tenant.service.create_branch).

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.core.ids import uuid7
from app.modules.templates.defaults import DEFAULT_RECEIPT_NAME, default_receipt_doc

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT c.id, c.locale, array_agg(b.id ORDER BY b.created_at, b.id) AS branch_ids
            FROM companies AS c
            JOIN branches AS b ON b.company_id = c.id AND b.deleted_at IS NULL AND b.is_active
            WHERE c.deleted_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM result_templates AS rt
                WHERE rt.company_id = c.id AND rt.deleted_at IS NULL AND rt.scope = 'receipt'
              )
            GROUP BY c.id, c.locale
            """
        )
    ).fetchall()
    insert = sa.text(
        """
        INSERT INTO result_templates
            (id, company_id, name, status, version, service_type_ids, category_ids, branch_ids, scope, language, doc, usage, created_at, updated_at)
        VALUES
            (:id, :cid, :name, 'draft', 1, '{}'::uuid[], '{}'::uuid[], :branch_ids, 'receipt', :lang, :doc, 0, now(), now())
        """
    ).bindparams(
        sa.bindparam("id", type_=postgresql.UUID(as_uuid=True)),
        sa.bindparam("cid", type_=postgresql.UUID(as_uuid=True)),
        sa.bindparam("branch_ids", type_=postgresql.ARRAY(postgresql.UUID(as_uuid=True))),
        sa.bindparam("doc", type_=postgresql.JSONB),
    )
    for cid, locale, branch_ids in rows:
        lang = locale if locale in DEFAULT_RECEIPT_NAME else "uz"
        bind.execute(insert, {"id": uuid7(), "cid": cid, "name": DEFAULT_RECEIPT_NAME[lang], "branch_ids": list(branch_ids), "lang": lang, "doc": default_receipt_doc("Receipt80", lang)})


def downgrade() -> None:
    # the seeded drafts are ordinary templates now (possibly edited) — they stay
    pass
