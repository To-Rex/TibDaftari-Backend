"""API tests for the patients module against the test database (fixture rows prefixed `T-patients-`).

Patients form one registry per country; lists are scoped by the facility that registered them (or the facilities
of their cheques)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.infrastructure.db.models import District, Patient
from app.modules.patients.service import invalidate_reference_cache
from fastapi.testclient import TestClient

from tests.helpers import API, COUNTRY_ID, app_instance, bearer, make_employee, make_tree, phone, seed, staff_token

RUN = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        emp_a = await make_employee(s, a.facility, "registrator", login=f"t-patients-a-{RUN}", password="secret123")
        emp_b = await make_employee(s, b.facility, "registrator", login=f"t-patients-b-{RUN}", password="secret123")
        admin_a = await make_employee(s, a.region, "admin", login=f"t-patients-admin-{RUN}", password="secret123")
        d2 = District(region_id=a.geo_region.id, name=f"T-{a.tag} tuman 2", sort_order=2, kind="city")
        other = Patient(country_id=COUNTRY_ID, registered_unit_id=b.facility.id, full_name=f"T-patients other {RUN}", phone=phone(), tags=[])
        s.add_all([d2, other])
        await s.flush()
        await invalidate_reference_cache()
        return {
            "a": {k: str(getattr(a, k).id) for k in ("country", "region", "district", "facility")},
            "b": {k: str(getattr(b, k).id) for k in ("country", "region", "district", "facility")},
            "tag": a.tag,
            "login_a": emp_a.login,
            "login_b": emp_b.login,
            "login_admin": admin_a.login,
            "country": str(COUNTRY_ID),
            "region": str(a.geo_region.id),
            "d1": str(a.geo_district.id),
            "d2": str(d2.id),
            "other_patient": str(other.id),
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        for key in ("a", "b", "admin"):
            data[f"h_{key}"] = bearer(staff_token(client, data[f"login_{key}"], "secret123"))
        yield data


def test_regions_and_districts_are_public(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    countries = c.get(f"{API}/countries").json()
    uz = next(x for x in countries if x["code"] == "UZ")
    assert uz["id"] == env["country"] and uz["isEnabled"] is True and uz["timezone"] == "Asia/Tashkent"
    assert set(uz) == {"id", "code", "name", "nameRu", "nameEn", "phoneCode", "currencyCode", "defaultLocale", "timezone", "phoneMask", "isEnabled"}
    regions = c.get(f"{API}/regions").json()  # no countryId → the default country (UZ)
    assert {"id": env["region"], "countryId": env["country"], "name": f"T-{env['tag']} viloyati"} in regions
    assert regions == c.get(f"{API}/regions", params={"countryId": env["country"]}).json()
    kz = next(x for x in countries if x["code"] == "KZ")
    kz_regions = c.get(f"{API}/regions", params={"countryId": kz["id"]}).json()
    assert kz_regions and all(r["countryId"] == kz["id"] for r in kz_regions)
    districts = c.get(f"{API}/districts", params={"regionId": env["region"]}).json()
    assert [d["id"] for d in districts] == [env["d1"], env["d2"]]
    assert set(districts[0]) == {"id", "regionId", "name", "kind"} and districts[0]["kind"] == "district" and districts[1]["kind"] == "city"
    # second call served from cache — same payload
    assert c.get(f"{API}/districts", params={"regionId": env["region"]}).json() == districts


def test_create_shape_and_normalisation(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    raw = phone()
    body = {
        "fullName": f"T-patients Xo‘jayev Shuhrat {RUN}",
        "phone": f"+{raw[:3]} {raw[3:5]} {raw[5:8]}-{raw[8:10]}-{raw[10:]}",
        "gender": "male",
        "birthDate": "1990-05-17",
        "passportNumber": f"aa{RUN[:7]}",
        "address": {"regionId": env["region"], "districtId": env["d1"], "street": "Navoiy 1"},
        "discountPercent": 150,
        "note": "  vip  ",
    }
    r = c.post(f"{API}/units/{env['a']['facility']}/patients", json=body, headers=env["h_a"])
    assert r.status_code == 201, r.text
    p = r.json()
    assert set(p) >= {"id", "countryId", "registeredUnitId", "registeredUnitName", "fullName", "phone", "address", "discountPercent", "tags", "stats", "portal", "createdAt", "updatedAt"}
    assert p["countryId"] == env["country"] and p["registeredUnitId"] == env["a"]["facility"] and p["registeredUnitName"] == f"T-{env['tag']} SES"
    assert p["phone"] == raw
    assert p["discountPercent"] == 100
    assert p["birthDate"] == "1990-05-17"
    assert p["address"] == {"regionId": env["region"], "districtId": env["d1"], "street": "Navoiy 1"}
    assert p["stats"] == {"orders": 0, "lastVisitAt": None, "totalSpent": 0}
    assert p["portal"] == {"linked": False, "telegramChatId": None, "login": None, "loginSetAt": None}
    assert p["tags"] == [] and p["note"] == "vip"
    assert p["createdAt"].endswith("Z")
    env["p1"] = p
    # from a higher unit the registering facility must be named (a facility under it)
    r = c.post(f"{API}/units/{env['a']['region']}/patients", json={"fullName": "T-patients NoUnit", "phone": phone()}, headers=env["h_admin"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "facility_required" and r.json()["error"]["message"] == "Bu amal faqat muassasa darajasida bajariladi"
    r = c.post(f"{API}/units/{env['a']['region']}/patients", json={"fullName": "T-patients Region", "phone": phone(), "registeredUnitId": env["a"]["facility"]}, headers=env["h_admin"])
    assert r.status_code == 201 and r.json()["registeredUnitId"] == env["a"]["facility"]
    r = c.post(f"{API}/units/{env['a']['region']}/patients", json={"fullName": "T-patients Elsewhere", "phone": phone(), "registeredUnitId": env["b"]["facility"]}, headers=env["h_admin"])
    assert r.status_code == 404 and r.json()["error"]["message"] == "Muassasa topilmadi"
    r = c.post(f"{API}/units/{env['a']['region']}/patients", json={"fullName": "T-patients District", "phone": phone(), "registeredUnitId": env["a"]["district"]}, headers=env["h_admin"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "facility_required"


def test_create_conflicts_and_validation(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    url = f"{API}/units/{env['a']['facility']}/patients"
    p1 = env["p1"]
    # passport, case-insensitive — country-wide: the other facility hits the same identity
    r = c.post(url, json={"fullName": "X", "phone": phone(), "passportNumber": p1["passportNumber"].upper()}, headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "duplicate_passport")
    r = c.post(f"{API}/units/{env['b']['facility']}/patients", json={"fullName": "X", "phone": phone(), "passportNumber": p1["passportNumber"]}, headers=env["h_b"])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "duplicate_passport")
    # pinfl
    pinfl = "3" + "".join(str(int(ch, 16) % 10) for ch in RUN) + "12345"[: 13 - len(RUN)]
    pinfl = (pinfl + "0" * 14)[:14]
    r = c.post(url, json={"fullName": "T-patients Pinfl one", "phone": phone(), "pinfl": pinfl}, headers=env["h_a"])
    assert r.status_code == 201, r.text
    r = c.post(url, json={"fullName": "T-patients Pinfl two", "phone": phone(), "pinfl": pinfl}, headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["message"]) == (409, "Bu JSHSHIR bilan bemor mavjud")
    # phone dup only when neither passport nor pinfl given
    r = c.post(url, json={"fullName": "T-patients Same phone", "phone": p1["phone"]}, headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "duplicate_phone")
    r = c.post(url, json={"fullName": "T-patients Same phone with pinfl", "phone": p1["phone"], "pinfl": "4" + pinfl[1:]}, headers=env["h_a"])
    assert r.status_code == 201, r.text
    env["p_pinfl"] = r.json()
    # invalid phone / pinfl
    r = c.post(url, json={"fullName": "Bad phone", "phone": "12345"}, headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["message"]) == (422, "invalid_phone", "Telefon raqam noto‘g‘ri")
    r = c.post(url, json={"fullName": "Bad pinfl", "phone": phone(), "pinfl": "123"}, headers=env["h_a"])
    assert r.status_code == 422


def test_list_search_sort_and_tag(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    url = f"{API}/units/{env['a']['facility']}/patients"
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
    # the region lists its facilities' patients and narrows by unitId; the other tree's facility matches nothing
    region_url = f"{API}/units/{env['a']['region']}/patients"
    assert c.get(region_url, params={"pageSize": 200}, headers=env["h_admin"]).json()["total"] == r.json()["total"]
    assert c.get(region_url, params={"unitId": env["a"]["facility"], "pageSize": 200}, headers=env["h_admin"]).json()["total"] == r.json()["total"]
    assert c.get(region_url, params={"unitId": env["b"]["facility"]}, headers=env["h_admin"]).json()["total"] == 0
    # tag filter after tagging p1 through update
    r = c.put(f"{API}/patients/{p1['id']}", json={"tags": ["vip"]}, headers=env["h_a"])
    assert r.status_code == 200 and r.json()["tags"] == ["vip"]
    r = c.get(url, params={"tag": "vip"}, headers=env["h_a"])
    assert [x["id"] for x in r.json()["items"]] == [p1["id"]]


def test_quick_search_and_duplicates(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    p1 = env["p1"]
    r = c.get(f"{API}/units/{env['a']['facility']}/patients/search", params={"q": p1["phone"][3:], "limit": 5}, headers=env["h_a"])
    assert r.status_code == 200 and isinstance(r.json(), list)
    assert {x["id"] for x in r.json()} == {p1["id"], env["p_pinfl"]["id"]}
    r = c.get(f"{API}/units/{env['a']['facility']}/patients/search", headers=env["h_a"])
    assert len(r.json()) >= 3
    # identity is country-wide: the other facility finds the patient by phone instead of registering a duplicate
    r = c.get(f"{API}/units/{env['b']['facility']}/patients/search", params={"q": p1["phone"]}, headers=env["h_b"])
    assert p1["id"] in {x["id"] for x in r.json()}
    r = c.post(
        f"{API}/units/{env['a']['facility']}/patients/duplicates",
        json={"phone": "+" + p1["phone"], "passportNumber": p1["passportNumber"].upper()},
        headers=env["h_a"],
    )
    assert {x["id"] for x in r.json()} == {p1["id"], env["p_pinfl"]["id"]}
    assert c.post(f"{API}/units/{env['a']['facility']}/patients/duplicates", json={}, headers=env["h_a"]).json() == []
    assert {x["id"] for x in c.post(f"{API}/units/{env['b']['facility']}/patients/duplicates", json={"phone": p1["phone"]}, headers=env["h_b"]).json()} == {p1["id"], env["p_pinfl"]["id"]}


def test_get_update_and_unit_isolation(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    p1 = env["p1"]
    r = c.get(f"{API}/patients/{p1['id']}", headers=env["h_a"])
    assert r.status_code == 200 and r.json()["fullName"] == p1["fullName"]
    # partial update: only note changes; phone re-normalised; passport clash excluding self is fine
    new_phone = phone()
    r = c.put(f"{API}/patients/{p1['id']}", json={"note": None, "phone": new_phone[3:], "passportNumber": p1["passportNumber"]}, headers=env["h_a"])
    assert r.status_code == 200, r.text
    assert r.json()["note"] is None and r.json()["phone"] == new_phone and r.json()["fullName"] == p1["fullName"]
    # taking another patient's pinfl → 409
    r = c.put(f"{API}/patients/{p1['id']}", json={"pinfl": env["p_pinfl"]["pinfl"]}, headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "duplicate_pinfl")
    # unknown id → 404 with the exact message
    r = c.get(f"{API}/patients/{uuid.uuid4()}", headers=env["h_a"])
    assert (r.status_code, r.json()["error"]["message"]) == (404, "Bemor topilmadi")
    # unit isolation: staff of facility B cannot see / edit / list facility A's patients
    assert c.get(f"{API}/patients/{p1['id']}", headers=env["h_b"]).status_code == 404
    assert c.put(f"{API}/patients/{p1['id']}", json={"note": "hack"}, headers=env["h_b"]).status_code == 404
    assert c.get(f"{API}/units/{env['a']['facility']}/patients", headers=env["h_b"]).status_code == 403
    r = c.get(f"{API}/units/{env['b']['facility']}/patients", headers=env["h_b"])
    assert [x["id"] for x in r.json()["items"]] == [env["other_patient"]]
    # the region administrator sees the facility's patients; statistics may be asked for one sub-unit
    assert c.get(f"{API}/patients/{p1['id']}", headers=env["h_admin"]).status_code == 200
    assert c.get(f"{API}/patients/{p1['id']}", params={"unitId": env["a"]["facility"]}, headers=env["h_admin"]).json()["stats"]["orders"] == 0
    assert c.get(f"{API}/patients/{p1['id']}", params={"unitId": env["b"]["facility"]}, headers=env["h_admin"]).status_code == 403
    assert c.get(f"{API}/patients/{p1['id']}").status_code == 401
