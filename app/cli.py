"""Operations CLI — ``python -m app.cli <command>``.

Commands
--------
* ``seed-reference``       upsert countries (``seed/reference/countries.json``) and regions / districts
                           (Uzbekistan's from ``uz-regions.json``, the neighbours' from ``countries.json``)
* ``create-country-root``  create the country node of the organisation tree when it is missing
                           (``--country UZ --name "O‘zbekiston Respublikasi"``) and switch the country on
* ``create-superadmin``    platform superadmin employee: no unit, the system ``superadmin`` role
* ``set-password``         reset a staff password by login
* ``ensure-partitions``    create upcoming monthly ``audit_log`` partitions (SQL function ``ensure_audit_partitions``)

Every command runs in one transaction: either it lands completely or nothing changes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.config import settings
from app.core.permissions import SUPERADMIN_ROLE_KEY, SYSTEM_ROLES
from app.core.security import hash_password
from app.core.textutil import fold
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Country, District, Employee, OrgUnit, Region, Role
from app.infrastructure.db.session import dispose_engine, session_scope

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "seed"
REFERENCE_FILE = SEED_DIR / "reference" / "uz-regions.json"
COUNTRIES_FILE = SEED_DIR / "reference" / "countries.json"

#: fixed id of the system superadmin role (db/v2/21_seed_roles.sql) — used only when the roles were not seeded
SUPERADMIN_ROLE_ID = uuid.UUID("01a00000-0000-7000-8000-000000000001")


class CliError(Exception):
    """User-facing failure: printed without a traceback, exit code 1."""


# --------------------------------------------------------------------------- helpers


def _load_json(path: Path) -> Any:
    import json

    if not path.exists():
        raise CliError(f"Seed file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def district_kind(item: dict[str, Any]) -> str:
    """`kind` of a district row: explicit in the file, else a city when the name says so (``… shahri``)."""
    kind = item.get("kind")
    if kind in ("district", "city"):
        return kind
    name = str(item.get("name") or "").strip().lower()
    return "city" if name.endswith(("shahri", "shahar", " sh.")) else "district"


# --------------------------------------------------------------------------- seed-reference


async def seed_reference(session: AsyncSession) -> tuple[int, int, int]:
    """Upsert countries (by ISO code), regions (by code; Uzbekistan's from uz-regions.json, the neighbours' from
    countries.json) and Uzbekistan districts (by region + name). Returns (countries, regions, districts)."""
    cdata = _load_json(COUNTRIES_FILE)
    countries_by_code: dict[str, Country] = {c.code: c for c in (await session.execute(select(Country))).scalars()}
    for i, item in enumerate(cdata["countries"]):
        row = countries_by_code.get(item["code"])
        if row is None:
            row = Country(code=item["code"], name=item["name"], name_ru=item.get("nameRu"), name_en=item.get("nameEn"), phone_code=item.get("phoneCode"), sort_order=int(item.get("order", i + 1)))
            session.add(row)
            countries_by_code[item["code"]] = row
        else:
            row.name, row.name_ru, row.name_en, row.phone_code = item["name"], item.get("nameRu"), item.get("nameEn"), item.get("phoneCode")
            row.sort_order = int(item.get("order", i + 1))
    await session.flush()
    uz = countries_by_code[settings.default_country_code]

    data = _load_json(REFERENCE_FILE)
    regions_by_code: dict[str, Region] = {
        r.code: r for r in (await session.execute(select(Region).where(Region.code.is_not(None)))).scalars()
    }
    region_items = [(uz, item) for item in data["regions"]] + [
        (countries_by_code[c["code"]], item) for c in cdata["countries"] for item in c.get("regions", [])
    ]
    for country, item in region_items:
        row = regions_by_code.get(item["code"])
        if row is None:
            row = Region(country_id=country.id, code=item["code"], name=item["name"], sort_order=int(item.get("order", 0)))
            session.add(row)
            regions_by_code[item["code"]] = row
        else:
            row.country_id = country.id
            row.name = item["name"]
            row.sort_order = int(item.get("order", 0))
    await session.flush()

    existing_districts: dict[tuple[uuid.UUID, str], District] = {
        (d.region_id, fold(d.name)): d for d in (await session.execute(select(District))).scalars()
    }
    for item in data["districts"]:
        region = regions_by_code.get(item["regionCode"])
        if region is None:
            raise CliError(f"District {item['name']!r} references unknown region code {item['regionCode']!r}")
        key = (region.id, fold(item["name"]))
        row = existing_districts.get(key)
        if row is None:
            row = District(region_id=region.id, name=item["name"], code=item.get("code"), sort_order=int(item.get("order", 0)), kind=district_kind(item))
            session.add(row)
            existing_districts[key] = row
        else:
            row.name = item["name"]
            row.sort_order = int(item.get("order", 0))
            row.kind = district_kind(item)
    await session.flush()
    return len(cdata["countries"]), len(region_items), len(data["districts"])


# --------------------------------------------------------------------------- organisation tree


async def _country_by_code(session: AsyncSession, code: str) -> Country:
    country = (await session.execute(select(Country).where(Country.code == code.strip().upper()))).scalar_one_or_none()
    if country is None:
        raise CliError(f"Country {code!r} not found in the reference data (run seed-reference first)")
    return country


async def create_country_root(*, country_code: str, name: str, short_name: str | None = None, code: str | None = None) -> tuple[str, bool]:
    """Create the country node (`org_units.level = 'country'`) of a country when it is missing and switch the country
    on (`countries.is_enabled`). Returns (unit id, created)."""
    async with session_scope() as session:
        country = await _country_by_code(session, country_code)
        existing = (await session.execute(select(OrgUnit).where(OrgUnit.level == "country", OrgUnit.country_id == country.id, alive(OrgUnit)))).scalar_one_or_none()
        if existing is not None:
            return str(existing.id), False
        unit = OrgUnit(
            level="country",
            parent_id=None,
            country_id=country.id,
            name=name.strip(),
            short_name=(short_name or "").strip() or None,
            code=(code or country.code).strip().upper(),
            is_active=True,
            sort_order=country.sort_order,
            settings={},
        )
        session.add(unit)
        country.is_enabled = True
        await session.flush()
        await audit(session, actor_type="system", actor_id=None, unit_id=unit.id, country_id=country.id, action="create", entity="unit", entity_id=unit.id, after={"level": "country", "name": unit.name, "code": unit.code, "countryCode": country.code})
        unit_id = str(unit.id)
    from app.modules.units.service import invalidate_tree_caches

    await invalidate_tree_caches(unit_id)
    return unit_id, True


# --------------------------------------------------------------------------- staff commands


async def _system_superadmin_role(session: AsyncSession) -> Role:
    """The system ``superadmin`` role (`scope_unit_id IS NULL`); created with its seed id when the roles were not seeded."""
    stmt = select(Role).where(Role.scope_unit_id.is_(None), Role.key == SUPERADMIN_ROLE_KEY, alive(Role))
    role = (await session.execute(stmt)).scalar_one_or_none()
    if role is None:
        spec = next(r for r in SYSTEM_ROLES if r["key"] == SUPERADMIN_ROLE_KEY)
        role = Role(id=SUPERADMIN_ROLE_ID, scope_unit_id=None, key=spec["key"], name=spec["name"], description="Platforma egasi — hamma narsa", permissions=list(spec["permissions"]), is_system=True)
        session.add(role)
        await session.flush()
    return role


async def create_superadmin(*, login: str, password: str, full_name: str) -> str:
    """Create a platform superadmin employee (no unit — sees everything). Returns the employee id."""
    async with session_scope() as session:
        taken = (await session.execute(select(Employee).where(func.lower(Employee.login) == login.lower(), alive(Employee)))).scalar_one_or_none()
        if taken is not None:
            raise CliError(f"Login '{login}' already exists (use set-password to reset its password)")
        role = await _system_superadmin_role(session)
        employee = Employee(unit_id=None, full_name=full_name, login=login, password_hash=hash_password(password), role_id=role.id, is_super_admin=True)
        session.add(employee)
        await session.flush()
        await audit(session, actor_type="system", actor_id=None, action="create", entity="employee", entity_id=employee.id, after={"login": login, "isSuperAdmin": True})
        return str(employee.id)


async def set_password(*, login: str, password: str) -> None:
    """Reset a staff password and clear the lock-out counters."""
    async with session_scope() as session:
        employee = (await session.execute(select(Employee).where(func.lower(Employee.login) == login.lower(), alive(Employee)))).scalar_one_or_none()
        if employee is None:
            raise CliError(f"Employee with login '{login}' not found")
        employee.password_hash = hash_password(password)
        employee.failed_logins = 0
        employee.locked_until = None
        await audit(session, actor_type="system", actor_id=None, unit_id=employee.unit_id, action="password_reset", entity="employee", entity_id=employee.id)


async def ensure_partitions(months_ahead: int = 3) -> None:
    """Create the next ``months_ahead`` monthly audit_log partitions (SQL function from db/v2/08_audit.sql)."""
    async with session_scope() as session:
        await session.execute(text("SELECT ensure_audit_partitions(:m)"), {"m": months_ahead})


# --------------------------------------------------------------------------- entrypoint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="TibDaftari operations CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed-reference", help="upsert countries + regions / districts from seed/reference")

    root = sub.add_parser("create-country-root", help="create the country node of the organisation tree (when missing) and enable the country")
    root.add_argument("--country", required=True, help="ISO 3166-1 alpha-2 code, e.g. UZ")
    root.add_argument("--name", required=True, help="node name, e.g. \"O‘zbekiston Respublikasi\"")
    root.add_argument("--short-name", default=None, help="short name used in message texts")
    root.add_argument("--code", default=None, help="node code (default: the country code)")

    sa = sub.add_parser("create-superadmin", help="create a platform superadmin employee (no unit, system superadmin role)")
    sa.add_argument("--login", required=True)
    sa.add_argument("--password", required=True)
    sa.add_argument("--name", required=True, help="employee full name")

    sp = sub.add_parser("set-password", help="reset a staff password")
    sp.add_argument("--login", required=True)
    sp.add_argument("--password", required=True)

    ep = sub.add_parser("ensure-partitions", help="create upcoming audit_log partitions")
    ep.add_argument("--months", type=int, default=3)
    return parser


async def _run(args: argparse.Namespace) -> None:
    try:
        if args.command == "seed-reference":
            async with session_scope() as session:
                countries, regions, districts = await seed_reference(session)
            print(f"reference: {countries} countries, {regions} regions, {districts} districts upserted")
        elif args.command == "create-country-root":
            unit_id, created = await create_country_root(country_code=args.country, name=args.name, short_name=args.short_name, code=args.code)
            print(f"country node {'created' if created else 'already exists'}: {args.country.upper()} ({unit_id})")
        elif args.command == "create-superadmin":
            employee_id = await create_superadmin(login=args.login, password=args.password, full_name=args.name)
            print(f"superadmin created: {args.login} ({employee_id})")
        elif args.command == "set-password":
            await set_password(login=args.login, password=args.password)
            print(f"password updated for {args.login}")
        elif args.command == "ensure-partitions":
            await ensure_partitions(args.months)
            print(f"audit_log partitions ensured for the next {args.months} months")
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint; returns the process exit code."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
