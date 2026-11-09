"""Shared fixtures of the v2 test-suite: the fixture organisation tree, employees, logins and DB access patterns.

Every test module builds its own tree with `make_tree()` — a `T-…` region / district pair in the geo reference
(so nothing clashes with the seeded territories), the matching region / district nodes under the seeded
O‘zbekiston country node and ONE facility (`code` = `T` + 5 random letters) — and homes its employees there with
`make_employee()`. `tests/conftest.py` soft-deletes it all at the end of the session.

Event loops: fixture rows are written in a plain `asyncio.run()` before the `TestClient` starts (`run_async` — it
disposes the engine and the in-process Redis afterwards, both are bound to the loop they were made in); DB reads
and writes during a test run on the app's own loop (`db(client, fn)`) or on a private engine (`own_engine()`).
"""

from __future__ import annotations

import asyncio
import random
import secrets
import string
import uuid
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from app.core.config import settings
from app.core.security import hash_password
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import District, Employee, EmployeeUnit, OrgUnit, Region, Role
from app.infrastructure.db.session import dispose_engine, session_scope
from app.infrastructure.redis.client import close_redis
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

API = "/api/v1"
#: O‘zbekiston — the country every fixture tree lives in (db/v2/20_seed_reference.sql)
COUNTRY_ID = uuid.UUID("01a0b069-7749-7d10-87b0-703892974a40")
#: the seeded country node (db/v2/22_seed_org.sql)
COUNTRY_UNIT_ID = uuid.UUID("01a1be00-0000-7000-8000-000000000001")
#: seeded region nodes (Xorazm viloyati / Toshkent shahri)
XORAZM_UNIT_ID = uuid.UUID("01a1be00-0000-7000-8000-000000000002")
TASHKENT_UNIT_ID = uuid.UUID("01a1be00-0000-7000-8000-000000000003")
DEFAULT_PASSWORD = "123456"

# the suite never talks to real workers / bots / SMS providers
settings.workers_enabled = False
settings.telegram_enabled = False
settings.otp_dev_mode = True


# ----------------------------------------------------------------------------- random bits


def rand_tag(n: int = 5) -> str:
    """`n` random uppercase letters — the facility code suffix and the `T-<tag> …` names of one tree."""
    return "".join(secrets.choice(string.ascii_uppercase) for _ in range(n))


def phone(prefix: str = "9989") -> str:
    """A random valid Uzbek number (12 digits)."""
    return prefix + "".join(random.choice("0123456789") for _ in range(12 - len(prefix)))


# ----------------------------------------------------------------------------- the tree


async def make_tree(session: AsyncSession, tag: str | None = None) -> SimpleNamespace:
    """Country node → `T-<tag>` region node → `T-<tag>` district node → one `ses` facility (`code` = `T<tag>`).

    Creates the region / district in the geo reference too (`T-<tag> viloyati` / `T-<tag> tumani`), so the partial
    unique indexes (one alive node per region / district) never clash with the seeded tree or another module's."""
    tg = (tag or rand_tag()).upper()
    region = Region(country_id=COUNTRY_ID, name=f"T-{tg} viloyati", sort_order=990)
    session.add(region)
    await session.flush()
    district = District(region_id=region.id, name=f"T-{tg} tumani", sort_order=1, kind="district")
    session.add(district)
    await session.flush()
    country = await session.get(OrgUnit, COUNTRY_UNIT_ID)
    assert country is not None, "the seeded country node is missing — load db/v2/22_seed_org.sql"
    region_unit = OrgUnit(level="region", parent_id=country.id, country_id=COUNTRY_ID, region_id=region.id, name=f"T-{tg} viloyati", short_name=f"T-{tg}", code=f"TR{tg}", sort_order=990, settings={})
    session.add(region_unit)
    await session.flush()
    district_unit = OrgUnit(level="district", parent_id=region_unit.id, country_id=COUNTRY_ID, region_id=region.id, district_id=district.id, name=f"T-{tg} tumani", code=f"TD{tg}", sort_order=1, settings={})
    session.add(district_unit)
    await session.flush()
    facility = await make_facility(session, district_unit, tag=tg)
    for u in (region_unit, district_unit):
        await session.refresh(u)  # timezone is filled by the DB trigger
    await invalidate_tree_caches()
    return SimpleNamespace(tag=tg, country=country, region=region_unit, district=district_unit, facility=facility, geo_region=region, geo_district=district)


async def make_facility(session: AsyncSession, district_unit: OrgUnit, *, tag: str | None = None, kind: str = "ses", name: str | None = None, **fields: Any) -> OrgUnit:
    """Another facility under a district node of a fixture tree (`code` = `T<tag>`)."""
    tg = (tag or rand_tag()).upper()
    unit = OrgUnit(
        level="facility",
        parent_id=district_unit.id,
        country_id=district_unit.country_id,
        region_id=district_unit.region_id,
        district_id=district_unit.district_id,
        kind=kind,
        name=name or f"T-{tg} SES",
        short_name=fields.pop("short_name", None),
        code=fields.pop("code", f"T{tg}"),
        settings=fields.pop("settings", {}),
        **fields,
    )
    session.add(unit)
    await session.flush()
    await session.refresh(unit)
    return unit


async def invalidate_tree_caches() -> None:
    """Drop the scope / principal / catalogue caches after the tree changed outside the API."""
    from app.modules.units.service import invalidate_tree_caches as _invalidate

    await _invalidate()


# ----------------------------------------------------------------------------- roles & employees


async def system_role(session: AsyncSession, key: str) -> Role:
    """One of the six seeded system roles (db/v2/21_seed_roles.sql)."""
    role = (await session.execute(select(Role).where(Role.scope_unit_id.is_(None), Role.key == key, alive(Role)))).scalar_one_or_none()
    assert role is not None, f"system role {key!r} is not seeded"
    return role


async def make_role(session: AsyncSession, unit: OrgUnit | None, key: str, name: str, permissions: Iterable[str], *, is_system: bool = False) -> Role:
    """A role scoped to `unit` (None = a system role) named `T-…` so the cleanup finds it."""
    role = Role(scope_unit_id=unit.id if unit is not None else None, key=key, name=name if name.startswith("T-") else f"T-{name}", permissions=list(permissions), is_system=is_system)
    session.add(role)
    await session.flush()
    return role


async def make_employee(
    session: AsyncSession,
    unit: OrgUnit | None,
    role_key: str = "admin",
    *,
    login: str | None = None,
    password: str = DEFAULT_PASSWORD,
    super_admin: bool = False,
    extra_units: Iterable[OrgUnit | uuid.UUID] = (),
    full_name: str | None = None,
    role: Role | None = None,
    **fields: Any,
) -> Employee:
    """An employee homed at `unit` with the system role `role_key` (or an explicit `role`). A superadmin has no
    unit. The clear-text password is kept on the row as `.password` for the login helpers."""
    role = role or await system_role(session, "superadmin" if super_admin else role_key)
    login = login or f"t-{role_key}-{uuid.uuid4().hex[:8]}"
    emp = Employee(
        unit_id=None if super_admin else (unit.id if unit is not None else None),
        full_name=full_name or f"T-{role_key} {login[-8:]}",
        login=login,
        password_hash=hash_password(password),
        role_id=role.id,
        is_super_admin=super_admin,
        **fields,
    )
    session.add(emp)
    await session.flush()
    for extra in extra_units:
        session.add(EmployeeUnit(employee_id=emp.id, unit_id=extra.id if isinstance(extra, OrgUnit) else extra))
    await session.flush()
    emp.password = password  # type: ignore[attr-defined]
    return emp


# ----------------------------------------------------------------------------- HTTP


def staff_token(client: TestClient, login: str, password: str = DEFAULT_PASSWORD) -> str:
    r = client.post(f"{API}/auth/staff/login", json={"login": login, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["accessToken"]


def staff_session(client: TestClient, login: str, password: str = DEFAULT_PASSWORD) -> dict[str, Any]:
    r = client.post(f"{API}/auth/staff/login", json={"login": login, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login_headers(client: TestClient, login: str, password: str = DEFAULT_PASSWORD) -> dict[str, str]:
    return bearer(staff_token(client, login, password))


def app_instance() -> Any:
    from app.main import app

    return app


# ----------------------------------------------------------------------------- event loops & engines


def run_async(fn: Callable[[], Awaitable[Any]] | Awaitable[Any]) -> Any:
    """Run a coroutine (or a coroutine function) on a fresh loop and release the loop-bound resources (engine pool,
    in-process Redis) afterwards — for fixture seeding before a `TestClient` starts, never inside one."""

    async def _main() -> Any:
        try:
            return await (fn() if callable(fn) else fn)
        finally:
            await close_redis()
            await dispose_engine()

    return asyncio.run(_main())


def seed(fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    """`run_async` + one committed `session_scope()` — the usual fixture shape: `data = seed(_rows)`."""

    async def _run() -> Any:
        async with session_scope() as s:
            return await fn(s)

    return run_async(_run)


def db(client: TestClient, fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    """Run `fn(session)` on the app's own event loop (shares the engine pool with the app) and commit."""

    async def _main() -> Any:
        async with session_scope() as s:
            return await fn(s)

    return client.portal.call(_main)


def own_engine() -> AsyncEngine:
    """A private engine for code that runs on its own loop while the TestClient's loop owns the shared one."""
    return create_async_engine(settings.sqlalchemy_url)


def own_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def own_session(engine: AsyncEngine):
    """One committed session on a private engine."""
    async with own_sessionmaker(engine)() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise
