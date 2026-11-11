"""Session-wide test hygiene for the v2 (organisation tree) suite.

Tests run against a throwaway database (`temodb_test`, never production) and build their own fixture tree with
`tests/helpers.make_tree()` under the seeded O‘zbekiston country node: a `T-…` region / district pair in the geo
reference, the matching region / district nodes and a facility whose code starts with `T`. Everything they create
is marked `T-` / `t-` and dated inside the run, so the end-of-session cleanup can find it:

* fixture units (and the rows hanging off them — employees, roles, patients, catalogue, templates, messaging
  settings, notifications) are SOFT-deleted, as every business row is in this system;
* the soft-deleted units are re-pointed at a real region / district so the `T-%` geo rows can be removed
  (reference lists must not fill up with fixture territories);
* the Redis namespaces the tree feeds (`unit:`, `principal:`, `roles:`, `catalog:`, `tpl:`, `ref:`) are dropped.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

RUN_STARTED = datetime.now(UTC)

#: a real region / district the soft-deleted fixture units are re-pointed at (Xorazm viloyati / Urganch shahri —
#: production reference ids, seeded by db/v2/20_seed_reference.sql)
REAL_REGION_ID = "01a00cdd-5629-7c75-9ba6-16755c8680d8"
REAL_DISTRICT_ID = "01a00cdd-5828-7d57-8b4f-56625ae76290"

_FIXTURE_UNITS = "SELECT id FROM org_units WHERE created_at >= :started AND (name LIKE 'T-%' OR code LIKE 'T%')"

_CLEANUP_SQL = [
    # the assistant's rows of the fixture tree (threads cascade to messages / actions)
    f"DELETE FROM assistant_threads WHERE unit_id IN ({_FIXTURE_UNITS}) OR employee_id IN (SELECT id FROM employees WHERE unit_id IN ({_FIXTURE_UNITS}) OR ((login LIKE 't-%' OR login LIKE 'T-%') AND created_at >= :started))",
    f"DELETE FROM assistant_accounts WHERE unit_id IN ({_FIXTURE_UNITS})",
    # people and roles of the fixture tree (+ anything named like a fixture row during this run)
    f"UPDATE employees SET deleted_at = now() WHERE deleted_at IS NULL AND (unit_id IN ({_FIXTURE_UNITS}) OR ((login LIKE 't-%' OR login LIKE 'T-%' OR full_name LIKE 'T-%') AND created_at >= :started))",
    f"UPDATE roles SET deleted_at = now() WHERE deleted_at IS NULL AND (scope_unit_id IN ({_FIXTURE_UNITS}) OR (name LIKE 'T-%' AND created_at >= :started))",
    f"UPDATE patients SET deleted_at = now() WHERE deleted_at IS NULL AND (registered_unit_id IN ({_FIXTURE_UNITS}) OR (full_name LIKE 'T-%' AND created_at >= :started))",
    # catalogue, templates and messaging settings owned by the fixture tree
    f"UPDATE categories SET deleted_at = now() WHERE deleted_at IS NULL AND owner_unit_id IN ({_FIXTURE_UNITS})",
    f"UPDATE service_types SET deleted_at = now() WHERE deleted_at IS NULL AND owner_unit_id IN ({_FIXTURE_UNITS})",
    f"UPDATE attribute_schemas SET deleted_at = now() WHERE deleted_at IS NULL AND owner_unit_id IN ({_FIXTURE_UNITS})",
    f"UPDATE result_templates SET deleted_at = now() WHERE deleted_at IS NULL AND owner_unit_id IN ({_FIXTURE_UNITS})",
    f"UPDATE template_assets SET deleted_at = now() WHERE deleted_at IS NULL AND owner_unit_id IN ({_FIXTURE_UNITS})",
    f"UPDATE messaging_accounts SET deleted_at = now(), is_active = false WHERE deleted_at IS NULL AND unit_id IN ({_FIXTURE_UNITS})",
    f"UPDATE sms_templates SET deleted_at = now(), is_active = false WHERE deleted_at IS NULL AND unit_id IN ({_FIXTURE_UNITS})",
    f"UPDATE notifications SET deleted_at = now() WHERE deleted_at IS NULL AND (unit_id IN ({_FIXTURE_UNITS}) OR (title LIKE 'T-%' AND created_at >= :started))",
    # the units themselves: soft-deleted and moved onto a real territory (root first — the DB trigger checks that a
    # child's territory matches its parent's) so the fixture geo rows below can go
    f"UPDATE org_units SET deleted_at = now(), is_active = false, region_id = :region, district_id = NULL WHERE level = 'region' AND id IN ({_FIXTURE_UNITS})",
    f"UPDATE org_units SET deleted_at = now(), is_active = false, region_id = :region, district_id = :district WHERE level = 'district' AND id IN ({_FIXTURE_UNITS})",
    f"UPDATE org_units SET deleted_at = now(), is_active = false, region_id = :region, district_id = :district WHERE level = 'facility' AND id IN ({_FIXTURE_UNITS})",
    # fixture geo rows (shared reference data, no soft delete) — only those nothing points at any more (a unit of
    # another run still using them is left alone; that run removes them itself)
    "UPDATE patients SET region_id = NULL, district_id = NULL WHERE (region_id IN (SELECT id FROM regions WHERE name LIKE 'T-%') OR district_id IN (SELECT d.id FROM districts d JOIN regions r ON r.id = d.region_id WHERE r.name LIKE 'T-%')) AND created_at >= :started",
    "DELETE FROM districts d WHERE d.region_id IN (SELECT id FROM regions WHERE name LIKE 'T-%') AND NOT EXISTS (SELECT 1 FROM org_units u WHERE u.district_id = d.id) AND NOT EXISTS (SELECT 1 FROM patients p WHERE p.district_id = d.id)",
    "DELETE FROM regions r WHERE r.name LIKE 'T-%' AND NOT EXISTS (SELECT 1 FROM org_units u WHERE u.region_id = r.id) AND NOT EXISTS (SELECT 1 FROM patients p WHERE p.region_id = r.id) AND NOT EXISTS (SELECT 1 FROM districts d WHERE d.region_id = r.id)",
]

_CACHE_PREFIXES = ("roles:", "unit:", "catalog:", "tpl:", "ref:", "principal:")


@pytest.fixture(scope="session", autouse=True)
def _soft_delete_fixture_tree():
    yield

    async def _run() -> None:
        from app.infrastructure.db.session import dispose_engine, session_scope
        from app.infrastructure.redis import cache
        from app.infrastructure.redis.client import close_redis

        try:
            async with session_scope() as s:
                for stmt in _CLEANUP_SQL:
                    await s.execute(text(stmt), {"started": RUN_STARTED, "region": REAL_REGION_ID, "district": REAL_DISTRICT_ID})
            for prefix in _CACHE_PREFIXES:
                await cache.delete_prefix(prefix)
        finally:
            await close_redis()
            await dispose_engine()

    asyncio.run(_run())
