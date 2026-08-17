"""API tests for the patients module against the real dev DB (fixture rows prefixed `T-patients-`)."""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import settings
from app.core.security import hash_password
from app.infrastructure.db.models import Company, District, Employee, Patient, Region, Role
from app.infrastructure.db.session import dispose_engine, session_scope
from app.infrastructure.redis.client import close_redis
from app.modules.patients.service import invalidate_reference_cache
from fastapi.testclient import TestClient

RUN = uuid.uuid4().hex[:8]
API = "/api/v1"


def _phone() -> str:
    return "99899" + "".join(random.choice("0123456789") for _ in range(7))


def _fixture_rows() -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        async with session_scope() as s:
            co_a = Company(name=f"T-patients-A-{RUN}", slug=f"t-patients-a-{RUN}")
            co_b = Company(name=f"T-patients-B-{RUN}", slug=f"t-patients-b-{RUN}")
            s.add_all([co_a, co_b])
            await s.flush()
            role_a = Role(company_id=co_a.id, key="registrator", name="T-patients", permissions=["reception.patient.read", "reception.patient.write"])
            role_b = Role(company_id=co_b.id, key="registrator", name="T-patients", permissions=["reception.patient.read", "reception.patient.write"])
            s.add_all([role_a, role_b])
            await s.flush()
            pw = hash_password("secret123")
            emp_a = Employee(company_id=co_a.id, full_name="T-patients A", login=f"t-patients-a-{RUN}", password_hash=pw, role_id=role_a.id)
            emp_b = Employee(company_id=co_b.id, full_name="T-patients B", login=f"t-patients-b-{RUN}", password_hash=pw, role_id=role_b.id)
            region = Region(name=f"T-patients viloyat {RUN}", order=999)
            s.add_all([emp_a, emp_b, region])
            await s.flush()
            d1 = District(region_id=region.id, name=f"T-patients tuman 1 {RUN}", order=1)
            d2 = District(region_id=region.id, name=f"T-patients tuman 2 {RUN}", order=2)
            other = Patient(company_id=co_b.id, full_name=f"T-patients other {RUN}", phone=_phone(), tags=[])
            s.add_all([d1, d2, other])
            await s.flush()
            await invalidate_reference_cache()
            return {
                "co_a": str(co_a.id),
                "co_b": str(co_b.id),
                "login_a": emp_a.login,
                "login_b": emp_b.login,
                "region": str(region.id),
                "d1": str(d1.id),
                "d2": str(d2.id),
                "other_patient": str(other.id),
            }

    async def _main() -> dict[str, Any]:
        try:
            return await _run()
        finally:
            await close_redis()
            await dispose_engine()

    return asyncio.run(_main())


def _build_app():
    """Full app when importable; otherwise a minimal app with the auth + patients routers (parallel module work)."""
    try:
        from app.main import app

        return app
    except ImportError:  # pragma: no cover - only while sibling modules are mid-rewrite
        from app.core.exceptions import install_exception_handlers
        from app.modules.auth.router import router as auth_router
        from app.modules.patients.router import router as patients_router
        from fastapi import APIRouter, FastAPI

        mini = FastAPI()
        install_exception_handlers(mini)
        api = APIRouter()
        api.include_router(auth_router, prefix="/auth")
        api.include_router(patients_router)
        mini.include_router(api, prefix=API)
        return mini


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    settings.workers_enabled = False
    settings.telegram_enabled = False
    data = _fixture_rows()
    with TestClient(_build_app()) as client:
        tok_a = client.post(f"{API}/auth/staff/login", json={"login": data["login_a"], "password": "secret123"}).json()["accessToken"]
        tok_b = client.post(f"{API}/auth/staff/login", json={"login": data["login_b"], "password": "secret123"}).json()["accessToken"]
        data["client"] = client
        data["h_a"] = {"Authorization": f"Bearer {tok_a}"}
        data["h_b"] = {"Authorization": f"Bearer {tok_b}"}
        yield data
    _cleanup_reference_rows(data["region"])


def _cleanup_reference_rows(region_id: str) -> None:
    """Regions/districts are shared reference data (no tenant, no soft delete) — remove the
    module's synthetic rows so repeated runs do not pollute the reference lists."""
    from sqlalchemy import delete

    async def _run() -> None:
        async with session_scope() as s:
            await s.execute(delete(District).where(District.region_id == uuid.UUID(region_id)))
            await s.execute(delete(Region).where(Region.id == uuid.UUID(region_id)))
            await invalidate_reference_cache()

    async def _main() -> None:
        try:
            await _run()
        finally:
            await close_redis()
            await dispose_engine()

    asyncio.run(_main())


def test_regions_and_districts_are_public(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    regions = c.get(f"{API}/regions").json()
    assert {"id": env["region"], "name": f"T-patients viloyat {RUN}"} in regions
    districts = c.get(f"{API}/districts", params={"regionId": env["region"]}).json()
    assert [d["id"] for d in districts] == [env["d1"], env["d2"]]
    assert set(districts[0]) == {"id", "regionId", "name"}
    # second call served from cache — same payload
    assert c.get(f"{API}/districts", params={"regionId": env["region"]}).json() == districts


def test_create_shape_and_normalisation(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    raw = _phone()
    body = {
        "fullName": f"Xo‘jayev Shuhrat {RUN}",
        "phone": f"+{raw[:3]} {raw[3:5]} {raw[5:8]}-{raw[8:10]}-{raw[10:]}",
        "gender": "male",
        "birthDate": "1990-05-17",
        "passportNumber": f"aa{RUN[:7]}",
        "address": {"regionId": env["region"], "districtId": env["d1"], "street": "Navoiy 1"},
        "discountPercent": 150,
        "note": "  vip  ",
    }
    r = c.post(f"{API}/companies/{env['co_a']}/patients", json=body, headers=env["h_a"])
    assert r.status_code == 201, r.text
    p = r.json()
    assert set(p) >= {"id", "companyId", "fullName", "phone", "address", "discountPercent", "tags", "stats", "portal", "createdAt", "updatedAt"}
    assert p["phone"] == raw
    assert p["discountPercent"] == 100
    assert p["birthDate"] == "1990-05-17"
    assert p["address"] == {"regionId": env["region"], "districtId": env["d1"], "street": "Navoiy 1"}
    assert p["stats"] == {"orders": 0, "lastVisitAt": None, "totalSpent": 0}
    assert p["portal"] == {"linked": False, "telegramChatId": None}
    assert p["tags"] == [] and p["note"] == "vip"
    assert p["createdAt"].endswith("Z")
    env["p1"] = p


def test_create_conflicts_and_validation(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    url = f"{API}/companies/{env['co_a']}/patients"
    p1 = env["p1"]
    # passport, case-insensitive
    r = c.post(url, json={"fullName": "X", "phone": _phone(), "passportNumber": p1["passportNumber"].upper()}, headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "duplicate_passport")
    # pinfl
    pinfl = "3" + "".join(random.choice("0123456789") for _ in range(13))
    r = c.post(url, json={"fullName": "Pinfl one", "phone": _phone(), "pinfl": pinfl}, headers=env["h_a"])
    assert r.status_code == 201, r.text
    r = c.post(url, json={"fullName": "Pinfl two", "phone": _phone(), "pinfl": pinfl}, headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["message"]) == (409, "Bu JSHSHIR bilan bemor mavjud")
    # phone dup only when neither passport nor pinfl given
    r = c.post(url, json={"fullName": "Same phone", "phone": p1["phone"]}, headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "duplicate_phone")
    r = c.post(url, json={"fullName": "Same phone with pinfl", "phone": p1["phone"], "pinfl": "4" + pinfl[1:]}, headers=env["h_a"])
    assert r.status_code == 201, r.text
    env["p_pinfl"] = r.json()
    # invalid phone / pinfl
    r = c.post(url, json={"fullName": "Bad phone", "phone": "12345"}, headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["message"]) == (422, "invalid_phone", "Telefon raqam noto‘g‘ri")
    r = c.post(url, json={"fullName": "Bad pinfl", "phone": _phone(), "pinfl": "123"}, headers=env["h_a"])
    assert r.status_code == 422


def test_list_search_sort_and_tag(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    url = f"{API}/companies/{env['co_a']}/patients"
    p1 = env["p1"]
    # folded name search (h → x, apostrophes stripped)
    r = c.get(url, params={"search": f"hojayev shuhrat {RUN}"}, headers=env["h_a"])
    assert r.status_code == 200 and [x["id"] for x in r.json()["items"]] == [p1["id"]]
    # phone digits (≥3) search
    r = c.get(url, params={"search": p1["phone"][-5:]}, headers=env["h_a"])
    assert p1["id"] in [x["id"] for x in r.json()["items"]]
    # passport search
    r = c.get(url, params={"search": p1["passportNumber"].upper()}, headers=env["h_a"])
    assert [x["id"] for x in r.json()["items"]] == [p1["id"]]
    # no match → empty page with totalPages 1
    r = c.get(url, params={"search": f"zzz-{RUN}-nomatch"}, headers=env["h_a"])
    assert r.json() == {"items": [], "page": 1, "pageSize": 20, "total": 0, "totalPages": 1}
    # default sort createdAt desc, then fullName asc
    r = c.get(url, params={"pageSize": 2}, headers=env["h_a"])
    ids = [x["id"] for x in r.json()["items"]]
    assert ids[0] == env["p_pinfl"]["id"] and r.json()["total"] >= 3
    r = c.get(url, params={"sortBy": "fullName", "sortDir": "asc", "pageSize": 200}, headers=env["h_a"])
    names = [x["fullName"] for x in r.json()["items"]]
    assert names == sorted(names)
    # tag filter after tagging p1 through update
    r = c.put(f"{API}/patients/{p1['id']}", json={"tags": ["vip"]}, headers=env["h_a"])
    assert r.status_code == 200 and r.json()["tags"] == ["vip"]
    r = c.get(url, params={"tag": "vip"}, headers=env["h_a"])
    assert [x["id"] for x in r.json()["items"]] == [p1["id"]]


def test_quick_search_and_duplicates(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    p1 = env["p1"]
    r = c.get(f"{API}/companies/{env['co_a']}/patients/search", params={"q": p1["phone"][3:], "limit": 5}, headers=env["h_a"])
    assert r.status_code == 200 and isinstance(r.json(), list)
    assert {x["id"] for x in r.json()} == {p1["id"], env["p_pinfl"]["id"]}
    r = c.get(f"{API}/companies/{env['co_a']}/patients/search", headers=env["h_a"])
    assert len(r.json()) >= 3
    r = c.post(
        f"{API}/companies/{env['co_a']}/patients/duplicates",
        json={"phone": "+" + p1["phone"], "passportNumber": p1["passportNumber"].upper()},
        headers=env["h_a"],
    )
    assert {x["id"] for x in r.json()} == {p1["id"], env["p_pinfl"]["id"]}
    assert c.post(f"{API}/companies/{env['co_a']}/patients/duplicates", json={}, headers=env["h_a"]).json() == []


def test_get_update_and_tenant_isolation(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    p1 = env["p1"]
    r = c.get(f"{API}/patients/{p1['id']}", headers=env["h_a"])
    assert r.status_code == 200 and r.json()["fullName"] == p1["fullName"]
    # partial update: only note changes; phone re-normalised; passport clash excluding self is fine
    new_phone = _phone()
    r = c.put(f"{API}/patients/{p1['id']}", json={"note": None, "phone": new_phone[3:], "passportNumber": p1["passportNumber"]}, headers=env["h_a"])
    assert r.status_code == 200, r.text
    assert r.json()["note"] is None and r.json()["phone"] == new_phone and r.json()["fullName"] == p1["fullName"]
    # taking another patient's pinfl → 409
    r = c.put(f"{API}/patients/{p1['id']}", json={"pinfl": env["p_pinfl"]["pinfl"]}, headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "duplicate_pinfl")
    # unknown id → 404 with the exact message
    r = c.get(f"{API}/patients/{uuid.uuid4()}", headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["message"]) == (404, "Bemor topilmadi")
    # tenant isolation: company B staff cannot see / edit / list company A
    assert c.get(f"{API}/patients/{p1['id']}", headers=env["h_b"]).status_code == 404
    assert c.put(f"{API}/patients/{p1['id']}", json={"note": "hack"}, headers=env["h_b"]).status_code == 404
    assert c.get(f"{API}/companies/{env['co_a']}/patients", headers=env["h_b"]).status_code == 403
    r = c.get(f"{API}/companies/{env['co_b']}/patients", headers=env["h_b"])
    assert [x["id"] for x in r.json()["items"]] == [env["other_patient"]]
    assert c.get(f"{API}/patients/{p1['id']}").status_code == 401
