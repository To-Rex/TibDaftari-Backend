"""Employee ↔ category binding: restricted staff only see/act on items of their categories (real dev DB, seeded)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    settings.workers_enabled = False
    settings.telegram_enabled = False
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _login(client: TestClient, login: str, password: str = "123456") -> dict:
    r = client.post(f"{API}/auth/staff/login", json={"login": login, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def test_restricted_employee_sees_only_own_categories(client: TestClient) -> None:
    admin = _login(client, "admin")
    h = {"Authorization": f"Bearer {admin['accessToken']}"}
    cid = admin["companyId"]
    cats = client.get(f"{API}/companies/{cid}/categories", headers=h).json()
    assert len(cats) >= 2
    cat_a, cat_b = cats[0], cats[1]
    roles = client.get(f"{API}/companies/{cid}/roles", headers=h).json()
    lab_role = next(r for r in roles if r["key"] == "laborant")

    login = f"t-catres-{uuid.uuid4().hex[:8]}"
    r = client.post(
        f"{API}/companies/{cid}/employees",
        headers=h,
        json={"fullName": "T-catres laborant", "login": login, "password": "123456", "roleId": lab_role["id"], "branchIds": [], "categoryIds": [cat_a["id"]]},
    )
    assert r.status_code in (200, 201), r.text
    assert r.json()["categoryIds"] == [cat_a["id"]]

    lab = _login(client, login)
    hl = {"Authorization": f"Bearer {lab['accessToken']}"}

    # worklist without a filter → only category A
    wl = client.get(f"{API}/companies/{cid}/worklist", headers=hl, params={"pageSize": 50}).json()
    assert wl["items"], "seed should contain worklist rows"
    assert {row["categoryId"] for row in wl["items"]} == {cat_a["id"]}
    # explicit filter on a foreign category → empty, not a leak
    wl_b = client.get(f"{API}/companies/{cid}/worklist", headers=hl, params={"pageSize": 5, "categoryIds": cat_b["id"]}).json()
    assert wl_b["total"] == 0 and wl_b["items"] == []

    # admin (unrestricted) sees category B rows; the restricted user may not touch them
    wl_admin_b = client.get(f"{API}/companies/{cid}/worklist", headers=h, params={"pageSize": 1, "categoryIds": cat_b["id"], "status": ["pending", "entered"]}).json()
    assert wl_admin_b["items"], "seed should contain pending/entered items in the second category"
    foreign = wl_admin_b["items"][0]
    r = client.put(f"{API}/items/{foreign['id']}/values", headers=hl, json={"values": foreign["values"], "labNote": "x"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["message"] == "Bu kategoriya sizga biriktirilmagan"

    # own-category item is editable
    own = next((row for row in wl["items"] if row["status"] in ("pending", "entered")), None)
    if own is not None:
        r = client.put(f"{API}/items/{own['id']}/values", headers=hl, json={"values": own["values"], "labNote": own.get("labNote")})
        assert r.status_code == 200, r.text


def test_worklist_counts_match_list_totals(client: TestClient) -> None:
    admin = _login(client, "admin")
    h = {"Authorization": f"Bearer {admin['accessToken']}"}
    cid = admin["companyId"]
    counts = client.get(f"{API}/companies/{cid}/worklist/counts", headers=h).json()
    assert set(counts) == {"all", "pending", "entered", "submitted", "approved", "rejected", "cancelled"}
    assert counts["all"] == sum(v for k, v in counts.items() if k != "all")
    submitted = client.get(f"{API}/companies/{cid}/worklist", headers=h, params={"pageSize": 1, "status": "submitted"}).json()
    assert submitted["total"] == counts["submitted"]
