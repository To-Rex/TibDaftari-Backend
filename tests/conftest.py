"""Session-wide test hygiene.

Tests run against the real dev database and create their own fixture companies (`T-*` / slug `t-*`).
Nothing is ever hard-deleted in this system, so at the end of the session those fixture tenants
(and everything hanging off them) are SOFT-deleted — they disappear from every list without
touching audit history. Real seed data (shifomed, nurklinika) is never matched.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

RUN_STARTED = datetime.now(UTC)

_CLEANUP_SQL = [
    # fixture companies created during this run
    "UPDATE companies SET deleted_at = now() WHERE deleted_at IS NULL AND (slug LIKE 't-%' OR name LIKE 'T-%') AND created_at >= :started",
    "UPDATE employees SET deleted_at = now() WHERE deleted_at IS NULL AND (company_id IN (SELECT id FROM companies WHERE deleted_at IS NOT NULL) OR ((login LIKE 't-%' OR full_name LIKE 'T-%') AND created_at >= :started))",
    "UPDATE roles SET deleted_at = now() WHERE deleted_at IS NULL AND (company_id IN (SELECT id FROM companies WHERE deleted_at IS NOT NULL) OR (name LIKE 'T-%' AND created_at >= :started))",
    "UPDATE branches SET deleted_at = now() WHERE deleted_at IS NULL AND company_id IN (SELECT id FROM companies WHERE deleted_at IS NOT NULL)",
    "UPDATE patients SET deleted_at = now() WHERE deleted_at IS NULL AND company_id IN (SELECT id FROM companies WHERE deleted_at IS NOT NULL)",
    "DELETE FROM districts WHERE region_id IN (SELECT id FROM regions WHERE name LIKE 'T-%')",
    "DELETE FROM regions WHERE name LIKE 'T-%'",
]


@pytest.fixture(scope="session", autouse=True)
def _soft_delete_test_tenants():
    yield

    async def _run() -> None:
        from app.infrastructure.db.session import dispose_engine, session_scope
        from app.infrastructure.redis import cache
        from app.infrastructure.redis.client import close_redis

        try:
            async with session_scope() as s:
                for stmt in _CLEANUP_SQL:
                    await s.execute(text(stmt), {"started": RUN_STARTED})
            await cache.delete_prefix("roles:")
            await cache.delete_prefix("co:")
            await cache.delete_prefix("ref:")
        finally:
            await close_redis()
            await dispose_engine()

    asyncio.run(_run())
