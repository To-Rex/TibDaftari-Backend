"""countries (ISO 3166-1) + regions.country_id + companies.country_id/region_id/district_id

Existing regions are Uzbekistan's; the country list and the neighbours' first-level regions come from
seed/reference/countries.json so a fresh deployment is usable without a separate seeding step.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-17
"""
from __future__ import annotations

import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.core.ids import uuid7

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None

REFERENCE = Path(__file__).resolve().parents[2] / "seed" / "reference" / "countries.json"


def upgrade() -> None:
    op.create_table(
        "countries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(2), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("name_ru", sa.String(120)),
        sa.Column("name_en", sa.String(120)),
        sa.Column("phone_code", sa.String(8)),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("code", name="uq_countries_code"),
    )
    data = json.loads(REFERENCE.read_text(encoding="utf-8"))
    ids: dict[str, object] = {}
    rows = []
    for i, c in enumerate(data["countries"]):
        ids[c["code"]] = uuid7()
        rows.append({"id": ids[c["code"]], "code": c["code"], "name": c["name"], "name_ru": c.get("nameRu"), "name_en": c.get("nameEn"), "phone_code": c.get("phoneCode"), "order": int(c.get("order", i + 1))})
    countries = sa.table("countries", sa.column("id"), sa.column("code"), sa.column("name"), sa.column("name_ru"), sa.column("name_en"), sa.column("phone_code"), sa.column("order"))
    op.bulk_insert(countries, rows)

    op.add_column("regions", sa.Column("country_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute(sa.text("UPDATE regions SET country_id = :uz WHERE country_id IS NULL").bindparams(sa.bindparam("uz", value=ids["UZ"], type_=postgresql.UUID(as_uuid=True))))
    op.alter_column("regions", "country_id", nullable=False)
    op.create_foreign_key("fk_regions_country_id", "regions", "countries", ["country_id"], ["id"])
    op.create_index("ix_regions_country_id", "regions", ["country_id"])
    regions = sa.table("regions", sa.column("id"), sa.column("country_id"), sa.column("name"), sa.column("code"), sa.column("order"))
    region_rows = [
        {"id": uuid7(), "country_id": ids[c["code"]], "name": r["name"], "code": r.get("code"), "order": int(r.get("order", k + 1))}
        for c in data["countries"]
        for k, r in enumerate(c.get("regions", []))
    ]
    if region_rows:
        op.bulk_insert(regions, region_rows)

    for col, ref in (("country_id", "countries"), ("region_id", "regions"), ("district_id", "districts")):
        op.add_column("companies", sa.Column(col, postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(f"fk_companies_{col}", "companies", ref, [col], ["id"])


def downgrade() -> None:
    for col in ("district_id", "region_id", "country_id"):
        op.drop_constraint(f"fk_companies_{col}", "companies", type_="foreignkey")
        op.drop_column("companies", col)
    op.drop_index("ix_regions_country_id", table_name="regions")
    op.drop_constraint("fk_regions_country_id", "regions", type_="foreignkey")
    op.drop_column("regions", "country_id")
    op.drop_table("countries")
