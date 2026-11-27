"""v2_0013 — the training again: the whole plan or chosen tasks, started by the employee or by a manager.

Applies `db/v2/43_training_repeat.sql` (idempotent) and records it in `schema_migrations`, exactly like the
baseline does for its scripts; a database where `run.sh` already applied 43 is left alone. Only `employee_training`
changes (real data — it is not one of the sandbox tables).

Revision ID: v2_0013
Revises: v2_0012
Create Date: 2026-11-27
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "v2_0013"
down_revision = "v2_0012"
branch_labels = None
depends_on = None

SCRIPTS = ("43_training_repeat.sql",)


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
    # nobody stays behind in a repeat they could have left: give them back the state they had
    conn.exec_driver_sql(
        "UPDATE employee_training SET status = previous->>'status', tasks = coalesce(previous->'tasks', '{}'::jsonb), "
        "completed_at = (previous->>'completedAt')::timestamptz, note = previous->>'note' "
        "WHERE previous IS NOT NULL AND status = 'active'"
    )
    conn.exec_driver_sql(
        "ALTER TABLE employee_training DROP COLUMN IF EXISTS chosen, DROP COLUMN IF EXISTS previous, "
        "DROP COLUMN IF EXISTS attempt, DROP COLUMN IF EXISTS started_by"
    )
    conn.exec_driver_sql("DELETE FROM schema_migrations WHERE version = '43'")
