"""v2_0009 — the company: the root of the organisation tree (Kompaniya → Davlat → Viloyat → Tuman → Muassasa).

Applies `db/v2/39_company_level.sql` (idempotent) and records it in `schema_migrations`, exactly like the baseline
does for its scripts; a database where `run.sh` already applied 39 is left alone. Everything that exists moves
under one company; ids, cheque numbers, settings, accounts and roles stay where they were.

Revision ID: v2_0009
Revises: v2_0008
Create Date: 2026-11-22
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "v2_0009"
down_revision = "v2_0008"
branch_labels = None
depends_on = None

SCRIPTS = ("39_company_level.sql",)


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


def _old(script: str, pattern: str) -> str:
    """A statement of the company-less schema, taken from the script that first defined it."""
    m = re.search(pattern, (_sql_dir() / script).read_text(encoding="utf-8"), re.S)
    assert m is not None, f"{pattern!r} not found in {script}"
    return m.group(0)


def downgrade() -> None:
    """Back to the company-less tree — possible only while ONE company exists and nothing but the countries hangs
    on it (staff, roles, catalogue or accounts placed on the company node make the delete below fail, and the
    whole downgrade with it)."""
    conn = op.get_bind()
    if (conn.execute(sa.text("SELECT count(*) FROM org_units WHERE level = 'company'")).scalar() or 0) > 1:
        raise RuntimeError("several companies exist — the tree no longer fits the company-less schema")
    validate = _old("02_org.sql", r"CREATE OR REPLACE FUNCTION org_units_validate\(\).*?END \$\$;")
    views = "\n".join(_old("09_views.sql", rf"CREATE VIEW {v} AS.*?;") for v in ("v_org_tree", "v_facilities", "v_org_unit_counts"))
    _run_script(
        conn,
        f"""
        DROP TRIGGER IF EXISTS trg_patients_company ON patients;
        DROP FUNCTION IF EXISTS patients_company();
        DROP VIEW IF EXISTS v_facilities;
        DROP VIEW IF EXISTS v_org_tree;
        DROP VIEW IF EXISTS v_org_unit_counts;
        DELETE FROM roles WHERE id = '01a00000-0000-7000-8000-00000000000c'
          AND NOT EXISTS (SELECT 1 FROM employees WHERE role_id = '01a00000-0000-7000-8000-00000000000c');
        ALTER TABLE org_units DISABLE TRIGGER trg_org_units_updated_at;
        ALTER TABLE org_units DISABLE TRIGGER trg_org_units_validate;
        ALTER TABLE org_units
          DROP CONSTRAINT IF EXISTS ck_org_units_level, DROP CONSTRAINT IF EXISTS ck_org_units_parent,
          DROP CONSTRAINT IF EXISTS ck_org_units_geo, DROP CONSTRAINT IF EXISTS ck_org_units_company_self;
        UPDATE org_units SET parent_id = NULL WHERE level = 'country';
        DROP INDEX IF EXISTS uq_org_units_country_alive;
        DROP INDEX IF EXISTS uq_org_units_region_alive;
        DROP INDEX IF EXISTS uq_org_units_district_alive;
        DROP INDEX IF EXISTS uq_org_units_code_alive;
        DROP INDEX IF EXISTS uq_org_units_company_name_alive;
        DROP INDEX IF EXISTS ix_org_units_company;
        DROP INDEX IF EXISTS uq_patients_country_passport_alive;
        DROP INDEX IF EXISTS uq_patients_country_pinfl_alive;
        DROP INDEX IF EXISTS ix_patients_country_phone;
        DROP INDEX IF EXISTS ix_patients_country_created;
        DROP INDEX IF EXISTS ix_patients_phone;
        ALTER TABLE patients DROP COLUMN IF EXISTS company_id;
        ALTER TABLE org_units DROP COLUMN IF EXISTS company_id;
        DELETE FROM org_units WHERE level = 'company';
        ALTER TABLE org_units ALTER COLUMN country_id SET NOT NULL;
        ALTER TABLE org_units
          ADD CONSTRAINT ck_org_units_level CHECK (level IN ('country', 'region', 'district', 'facility')),
          ADD CONSTRAINT ck_org_units_parent CHECK ((level = 'country') = (parent_id IS NULL)),
          ADD CONSTRAINT ck_org_units_geo CHECK (
               (level = 'country'  AND region_id IS NULL     AND district_id IS NULL)
            OR (level = 'region'   AND region_id IS NOT NULL AND district_id IS NULL)
            OR (level IN ('district', 'facility') AND region_id IS NOT NULL AND district_id IS NOT NULL));
        CREATE UNIQUE INDEX uq_org_units_country_alive  ON org_units (country_id)  WHERE level = 'country'  AND deleted_at IS NULL;
        CREATE UNIQUE INDEX uq_org_units_region_alive   ON org_units (region_id)   WHERE level = 'region'   AND deleted_at IS NULL;
        CREATE UNIQUE INDEX uq_org_units_district_alive ON org_units (district_id) WHERE level = 'district' AND deleted_at IS NULL;
        CREATE UNIQUE INDEX uq_org_units_code_alive     ON org_units (lower(code)) WHERE code IS NOT NULL AND deleted_at IS NULL;
        CREATE UNIQUE INDEX uq_patients_country_passport_alive ON patients (country_id, passport_number)
          WHERE deleted_at IS NULL AND passport_number IS NOT NULL;
        CREATE UNIQUE INDEX uq_patients_country_pinfl_alive ON patients (country_id, pinfl)
          WHERE deleted_at IS NULL AND pinfl IS NOT NULL;
        CREATE INDEX ix_patients_country_phone   ON patients (country_id, phone);
        CREATE INDEX ix_patients_country_created ON patients (country_id, created_at);
        {validate}
        ALTER TABLE org_units ENABLE TRIGGER trg_org_units_validate;
        ALTER TABLE org_units ENABLE TRIGGER trg_org_units_updated_at;
        {views}
        DELETE FROM schema_migrations WHERE version = '39';
        """,
    )
