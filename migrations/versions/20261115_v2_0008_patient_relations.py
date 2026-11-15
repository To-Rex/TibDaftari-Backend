"""v2_0008 — one phone, a whole family: undocumented patients may share a number; `relation` + `related_patient_id`.

Applies `db/v2/38_patient_relations.sql` (idempotent) and records it in `schema_migrations`, exactly like the
baseline does for its scripts; a database where `run.sh` already applied 38 is left alone.

Revision ID: v2_0008
Revises: v2_0007
Create Date: 2026-11-15
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "v2_0008"
down_revision = "v2_0007"
branch_labels = None
depends_on = None

SCRIPTS = ("38_patient_relations.sql",)


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
    conn.exec_driver_sql("DROP INDEX IF EXISTS ix_patients_related")
    conn.exec_driver_sql(
        "ALTER TABLE patients DROP CONSTRAINT IF EXISTS ck_patients_relation_self, "
        "DROP CONSTRAINT IF EXISTS ck_patients_relation_pair, DROP CONSTRAINT IF EXISTS ck_patients_relation"
    )
    conn.exec_driver_sql("ALTER TABLE patients DROP COLUMN IF EXISTS relation, DROP COLUMN IF EXISTS related_patient_id")
    # the one-patient-per-number rule comes back only when the data still allows it (families stay otherwise)
    conn.exec_driver_sql(
        "DO $$ BEGIN "
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_country_phone_noid_alive ON patients (country_id, phone) "
        "WHERE deleted_at IS NULL AND passport_number IS NULL AND pinfl IS NULL; "
        "EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'shared numbers exist — the unique phone index is not restored'; END $$"
    )
    conn.exec_driver_sql("DELETE FROM schema_migrations WHERE version = '38'")
