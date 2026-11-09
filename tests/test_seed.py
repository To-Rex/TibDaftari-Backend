"""The operations CLI: argument parsing (pure) and the `create-superadmin` / `create-country-root` /
`seed-reference` commands against the test database."""

from __future__ import annotations

import uuid

import pytest
from app.cli import CliError, build_parser, create_country_root, create_superadmin, district_kind, seed_reference
from app.infrastructure.db.models import Country, Employee, OrgUnit, Role
from app.infrastructure.db.session import SessionLocal, session_scope
from sqlalchemy import select

from tests.helpers import COUNTRY_ID, COUNTRY_UNIT_ID, run_async

SFX = uuid.uuid4().hex[:8]


def test_parser_knows_every_command() -> None:
    args = build_parser().parse_args(["seed-reference"])
    assert args.command == "seed-reference"
    args = build_parser().parse_args(["create-superadmin", "--login", "root", "--password", "pw", "--name", "Root"])
    assert (args.command, args.login, args.password, args.name) == ("create-superadmin", "root", "pw", "Root")
    args = build_parser().parse_args(["create-country-root", "--country", "uz", "--name", "O‘zbekiston Respublikasi", "--short-name", "O‘zbekiston"])
    assert (args.command, args.country, args.short_name, args.code) == ("create-country-root", "uz", "O‘zbekiston", None)
    args = build_parser().parse_args(["set-password", "--login", "a", "--password", "b"])
    assert args.command == "set-password"
    assert build_parser().parse_args(["ensure-partitions", "--months", "5"]).months == 5
    with pytest.raises(SystemExit):  # the demo seed is gone
        build_parser().parse_args(["seed-demo"])


def test_district_kind_is_explicit_or_guessed_from_the_name() -> None:
    assert district_kind({"name": "Urganch shahri"}) == "city"
    assert district_kind({"name": "Bog‘ot tumani"}) == "district"
    assert district_kind({"name": "Urganch shahri", "kind": "district"}) == "district"
    assert district_kind({"name": "X", "kind": "weird"}) == "district"


def test_create_superadmin_uses_the_system_role_and_refuses_a_taken_login() -> None:
    login = f"t-cli-super-{SFX}"

    async def flow() -> tuple[Employee, Role]:
        emp_id = await create_superadmin(login=login, password="secret-1", full_name="T-cli Super")
        with pytest.raises(CliError):
            await create_superadmin(login=login.upper(), password="x", full_name="T-cli Dup")
        async with session_scope() as s:
            emp = await s.get(Employee, uuid.UUID(emp_id))
            role = await s.get(Role, emp.role_id)
            return emp, role

    emp, role = run_async(flow)
    assert emp.is_super_admin is True and emp.unit_id is None and emp.login == login
    assert role.key == "superadmin" and role.scope_unit_id is None and role.is_system is True


def test_create_country_root_is_idempotent_for_a_seeded_country() -> None:
    async def flow() -> tuple[tuple[str, bool], Country]:
        out = await create_country_root(country_code="uz", name="O‘zbekiston Respublikasi")
        async with session_scope() as s:
            return out, await s.get(Country, COUNTRY_ID)

    (unit_id, created), country = run_async(flow)
    assert created is False and uuid.UUID(unit_id) == COUNTRY_UNIT_ID
    assert country.is_enabled is True
    with pytest.raises(CliError):
        run_async(create_country_root(country_code="ZZ", name="Nowhere"))


def test_seed_reference_upserts_without_duplicating() -> None:
    """Re-running the reference seed on a seeded database changes nothing (rolled back here anyway)."""

    async def flow() -> tuple[tuple[int, int, int], int, int]:
        async with SessionLocal() as s:
            before = (await s.execute(select(OrgUnit.id).where(OrgUnit.level == "country"))).scalars().all()
            counts = await seed_reference(s)
            n_countries = (await s.execute(select(Country.id))).scalars().all()
            await s.rollback()
        return counts, len(n_countries), len(before)

    counts, n_countries, n_roots = run_async(flow)
    assert counts[0] >= 1 and counts[1] >= 14 and counts[2] >= 100
    assert n_countries == counts[0] and n_roots >= 1
