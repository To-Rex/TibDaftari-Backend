"""v2_0006 — the four read-only leader roles («Davlat / Viloyat / Tuman / Muassasa rahbari»).

Applies `db/v2/36_leader_roles.sql` (idempotent) and records it in `schema_migrations`, exactly like the
baseline does for its scripts; a database where `run.sh` already applied 36 is left alone.

Revision ID: v2_0006
Revises: v2_0005
Create Date: 2026-11-12
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "v2_0006"
down_revision = "v2_0005"
branch_labels = None
depends_on = None

SCRIPTS = ("36_leader_roles.sql",)


def _sql_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "db" / "v2"


def _run_script(conn, body: str) -> None:
    """Run a multi-statement SQL script. Under asyncpg a prepared statement may hold ONE command, so the script goes
    through the driver's simple-query protocol (`asyncpg.Connection.execute` without arguments); other drivers
    take it as is."""
    if conn.dialect.driver == "asyncpg":
        from sqlalchemy.util import await_only

        await_only(conn.connection.driver_connection.execute(body))
    else:
        conn.exec_driver_sql(body)


def upgrade() -> None:
    conn = op.get_bind()
    applied = {row[0] for row in conn.execute(sa.text("SELECT version FROM schema_migrations")).all()}
    for name in SCRIPTS:
        version = name.split("_", 1)[0]
        if version in applied:
            continue
        body = (_sql_dir() / name).read_text(encoding="utf-8")
        _run_script(conn, body)
        conn.execute(
            sa.text("INSERT INTO schema_migrations (version, name, checksum) VALUES (:v, :n, :c) ON CONFLICT (version) DO NOTHING"),
            {"v": version, "n": name.split("_", 1)[1].removesuffix(".sql"), "c": hashlib.sha256(body.encode("utf-8")).hexdigest()},
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "UPDATE roles SET deleted_at = now() WHERE scope_unit_id IS NULL AND deleted_at IS NULL "
        "AND key IN ('leader_country', 'leader_region', 'leader_district', 'leader_facility')"
    )
    conn.exec_driver_sql("DELETE FROM schema_migrations WHERE version = '36'")
