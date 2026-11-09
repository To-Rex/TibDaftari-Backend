"""v2 baseline — the organisation-tree schema (Davlat → Viloyat → Tuman/Shahar → Muassasa).

The schema is defined by the SQL scripts in `db/v2/` (one source of truth for the database, the app and the
staging/production instances). This revision applies the DDL scripts and the reference seeds (geo + system roles)
on an empty database; a database already shaped by `db/v2/run.sh` is simply stamped (`alembic stamp v2_0001`).

Revision ID: v2_0001
Revises:
Create Date: 2026-11-09
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "v2_0001"
down_revision = None
branch_labels = None
depends_on = None

SCRIPTS = (
    "00_foundation.sql",
    "01_reference.sql",
    "02_org.sql",
    "03_staff.sql",
    "04_patients.sql",
    "05_catalog.sql",
    "06_orders.sql",
    "07_messaging.sql",
    "08_audit.sql",
    "09_views.sql",
    "10_messaging_env_bot.sql",
    "20_seed_reference.sql",
    "21_seed_roles.sql",
    "31_permissions_rename.sql",
    "32_installer_role.sql",
)


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
    applied = set()
    if conn.dialect.has_table(conn, "schema_migrations"):
        applied = {row[0] for row in conn.execute(sa.text("SELECT version FROM schema_migrations")).all()}
    for name in SCRIPTS:
        version = name.split("_", 1)[0]
        if version in applied:
            continue
        path = _sql_dir() / name
        body = path.read_text(encoding="utf-8")
        _run_script(conn, body)
        conn.execute(
            sa.text("INSERT INTO schema_migrations (version, name, checksum) VALUES (:v, :n, :c) ON CONFLICT (version) DO NOTHING"),
            {"v": version, "n": name.split("_", 1)[1].removesuffix(".sql"), "c": hashlib.sha256(body.encode("utf-8")).hexdigest()},
        )


def downgrade() -> None:
    raise NotImplementedError("the v2 baseline cannot be downgraded — drop the database instead")
