"""API tests for the units module (the organisation tree) against the test database: tree and card, creating
child units with geo validation and codes, profile / settings updates, inherited SMS accounts and texts, the test
SMS, Telegram bots and the superadmin reset. Fixture rows are prefixed `T-`."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.infrastructure.db.models import Region
from app.modules.messaging import service as messaging
from app.modules.messaging.xabarchi import ProviderResult, XabarchiError
from app.modules.units import service as units_service
from app.modules.units.reset import FACILITY_PARTS
from fastapi.testclient import TestClient

from tests.helpers import (
    API,
    COUNTRY_ID,
    COUNTRY_UNIT_ID,
    app_instance,
    bearer,
    db,
    make_employee,
    make_tree,
    phone,
    seed,
    staff_token,
)


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        # a second free region of the geo reference for the "create a region node" test
        spare = Region(country_id=COUNTRY_ID, name=f"T-{a.tag}2 viloyati", sort_order=991)
        s.add(spare)
        await s.flush()
        sup = await make_employee(s, None, super_admin=True)
        admin_a = await make_employee(s, a.region, "admin")
        admin_b = await make_employee(s, b.region, "admin")
        return {
            "a": {k: str(getattr(a, k).id) for k in ("country", "region", "district", "facility")},
            "b": {k: str(getattr(b, k).id) for k in ("country", "region", "district", "facility")},
            "tag": a.tag,
            "code_a": a.facility.code,
            "spare_region": str(spare.id),
            "geo_region_a": str(a.geo_region.id),
            "geo_district_a": str(a.geo_district.id),
            "logins": {"super": sup.login, "admin-a": admin_a.login, "admin-b": admin_b.login},
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, login) for who, login in data["logins"].items()}
        data["state"] = {}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


def test_tree_and_card(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["a"], ctx["b"]
    # superadmin: the whole tree; a region administrator: their subtree + the path above it, never the other tree
    everything = {u["id"] for u in c.get(f"{API}/units/tree", headers=h(ctx, "super")).json()}
    assert set(a.values()) <= everything and set(b.values()) <= everything
    mine = c.get(f"{API}/units/tree", headers=h(ctx, "admin-a"))
    assert mine.status_code == 200, mine.text
    ids = {u["id"] for u in mine.json()}
    assert {a["country"], a["region"], a["district"], a["facility"]} <= ids and not ({b["region"], b["district"], b["facility"]} & ids)
    by_id = {u["id"]: u for u in mine.json()}
    assert by_id[a["facility"]]["parentId"] == a["district"] and by_id[a["region"]]["parentId"] == a["country"] and by_id[a["country"]]["parentId"] is None
    assert [by_id[a[k]]["depth"] for k in ("country", "region", "district", "facility")] == [0, 1, 2, 3]
    # the card
    r = c.get(f"{API}/units/{a['facility']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["level"] == "facility" and d["kind"] == "ses" and d["code"] == ctx["code_a"] and d["parentId"] == a["district"]
    assert d["countryId"] == str(COUNTRY_ID) and d["regionId"] == ctx["geo_region_a"] and d["districtId"] == ctx["geo_district_a"]
    assert d["countryName"] == "O‘zbekiston" and d["regionName"] == f"T-{ctx['tag']} viloyati" and d["districtName"] == f"T-{ctx['tag']} tumani"
    assert d["path"] == f"O‘zbekiston Respublikasi › T-{ctx['tag']} viloyati › T-{ctx['tag']} tumani › T-{ctx['tag']} SES" and d["depth"] == 3
    assert d["timezone"] == "Asia/Tashkent" and d["isActive"] is True and d["settings"] == {"documentFreeViewDays": 0, "locale": None, "own": {}}
    assert d["sms"] == {"mode": "inherited", "provider": "none", "apiKeyMasked": None, "defaultPriority": "transactional", "senderNote": None, "effectiveUnitId": None, "effectiveUnitName": None}
    assert d["telegram"] == {"botUsername": None, "connected": False, "own": False, "effectiveUnitId": None}
    assert d["counts"] == {"regions": 0, "districts": 0, "facilities": 0, "employees": 0}
    region = c.get(f"{API}/units/{a['region']}", headers=h(ctx, "admin-a")).json()
    assert region["counts"] == {"regions": 0, "districts": 1, "facilities": 1, "employees": 1} and region["depth"] == 1
    # isolation: another tree → 403, the path above → readable context, unknown → 404
    r = c.get(f"{API}/units/{b['facility']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 403 and r.json()["error"]["message"] == "Ruxsat yo‘q"
    assert c.get(f"{API}/units/{a['country']}", headers=h(ctx, "admin-a")).status_code == 200
    r = c.get(f"{API}/units/{uuid.uuid4()}", headers=h(ctx, "super"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Birlik topilmadi"
    assert c.get(f"{API}/units/{a['facility']}").status_code == 401


def test_create_child_units_geo_and_codes(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a = ctx["a"]
    tag = ctx["tag"]
    countries = c.get(f"{API}/countries").json()
    kz = next(x for x in countries if x["code"] == "KZ")
    kz_region = c.get(f"{API}/regions", params={"countryId": kz["id"]}).json()[0]
    # a region node under the country (platform admin): the region must belong to the country
    r = c.post(f"{API}/units", json={"parentId": a["country"], "regionId": kz_region["id"], "name": f"T-{tag}2 viloyati"}, headers=h(ctx, "super"))
    assert r.status_code == 422 and r.json()["error"]["message"] == "Viloyat tanlangan davlatga tegishli emas"
    r = c.post(f"{API}/units", json={"parentId": a["country"], "regionId": ctx["spare_region"], "name": f"T-{tag}2 viloyati", "code": f"tr{tag}2"}, headers=h(ctx, "super"))
    assert r.status_code == 201, r.text
    region2 = r.json()
    assert region2["level"] == "region" and region2["regionId"] == ctx["spare_region"] and region2["code"] == f"TR{tag}2" and region2["depth"] == 1
    assert region2["parentId"] == str(COUNTRY_UNIT_ID) and region2["path"] == f"O‘zbekiston Respublikasi › T-{tag}2 viloyati"
    # one alive node per region
    r = c.post(f"{API}/units", json={"parentId": a["country"], "regionId": ctx["spare_region"], "name": "T-dup"}, headers=h(ctx, "super"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "node_exists"
    # a district node: the district must belong to the parent's region
    r = c.post(f"{API}/units", json={"parentId": region2["id"], "districtId": ctx["geo_district_a"], "name": "T-wrong tumani"}, headers=h(ctx, "super"))
    assert r.status_code == 422 and r.json()["error"]["message"] == "Tuman tanlangan viloyatga tegishli emas"
    assert c.post(f"{API}/units", json={"parentId": region2["id"], "districtId": str(uuid.uuid4()), "name": "T-none"}, headers=h(ctx, "super")).status_code == 422
    # a facility under the district: kind and code are required, the code is unique (case-insensitive) and upper-cased
    r = c.post(f"{API}/units", json={"parentId": a["district"], "name": f"T-{tag} klinika"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "kind_required"
    r = c.post(f"{API}/units", json={"parentId": a["district"], "name": f"T-{tag} klinika", "kind": "clinic"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "code_required"
    r = c.post(f"{API}/units", json={"parentId": a["district"], "name": f"T-{tag} klinika", "kind": "clinic", "code": ctx["code_a"].lower()}, headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["message"] == "Bu kod band"
    r = c.post(f"{API}/units", json={"parentId": a["district"], "name": f"T-{tag} klinika", "kind": "clinic", "code": f"t{tag}k", "phone": "998901234567"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    clinic = r.json()
    assert clinic["level"] == "facility" and clinic["kind"] == "clinic" and clinic["code"] == f"T{tag}K" and clinic["timezone"] == "Asia/Tashkent"
    assert (clinic["regionId"], clinic["districtId"]) == (ctx["geo_region_a"], ctx["geo_district_a"]) and clinic["depth"] == 3 and clinic["path"].endswith(f"› T-{tag} klinika")
    ctx["state"]["clinic"] = clinic["id"]
    # the parent's counts follow; nothing hangs under a facility; other trees are out of reach
    assert c.get(f"{API}/units/{a['district']}", headers=h(ctx, "admin-a")).json()["counts"]["facilities"] == 2
    r = c.post(f"{API}/units", json={"parentId": clinic["id"], "name": "x", "kind": "ses", "code": "XX1"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "leaf"
    assert c.post(f"{API}/units", json={"parentId": a["district"], "name": "T-hack", "kind": "ses", "code": "THACK"}, headers=h(ctx, "admin-b")).status_code == 403
    assert c.post(f"{API}/units", json={"countryId": str(COUNTRY_ID), "name": "T-root"}, headers=h(ctx, "admin-a")).status_code == 403
    # the first facility of an organisation brings the standard cheque with it (a draft receipt bound to it)
    templates = c.get(f"{API}/units/{a['district']}/templates", headers=h(ctx, "admin-a")).json()
    receipts = [t for t in templates if t["scope"] == "receipt"]
    assert len(receipts) == 1 and receipts[0]["status"] == "draft" and receipts[0]["name"] == "Standart chek" and receipts[0]["unitIds"] == [clinic["id"]]
    assert receipts[0]["ownerUnitId"] == a["district"]
    r = c.post(f"{API}/units", json={"parentId": a["district"], "name": f"T-{tag} lab", "kind": "laboratory", "code": f"T{tag}L"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    assert len([t for t in c.get(f"{API}/units/{a['district']}/templates", headers=h(ctx, "admin-a")).json() if t["scope"] == "receipt"]) == 1


def test_update_unit_profile_and_settings(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["a"], ctx["b"]
    r = c.put(f"{API}/units/{a['facility']}", json={"name": f"T-{ctx['tag']} SES 2", "phone": "998901112233", "isActive": False, "shortName": "T-SES2"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == f"T-{ctx['tag']} SES 2" and r.json()["phone"] == "998901112233" and r.json()["isActive"] is False and r.json()["shortName"] == "T-SES2"
    assert c.put(f"{API}/units/{a['facility']}", json={"isActive": True}, headers=h(ctx, "admin-a")).json()["isActive"] is True
    # the code stays unique among alive units
    r = c.put(f"{API}/units/{a['facility']}", json={"code": ctx["code_a"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["code"] == ctx["code_a"]
    other = c.get(f"{API}/units/{b['facility']}", headers=h(ctx, "super")).json()["code"]
    r = c.put(f"{API}/units/{a['facility']}", json={"code": other.lower()}, headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["message"] == "Bu kod band"
    r = c.put(f"{API}/units/{uuid.uuid4()}", json={"name": "x"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Birlik topilmadi"
    assert c.put(f"{API}/units/{a['facility']}", json={"name": "hack"}, headers=h(ctx, "admin-b")).status_code == 403
    # settings inherit root → leaf: set on the region, effective on the facility; the facility's own stays empty
    r = c.put(f"{API}/units/{a['region']}", json={"documentFreeViewDays": 3, "locale": "ru"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["settings"] == {"documentFreeViewDays": 3, "locale": "ru", "own": {"documentFreeViewDays": 3, "locale": "ru"}}
    fac = c.get(f"{API}/units/{a['facility']}", headers=h(ctx, "admin-a")).json()["settings"]
    assert fac == {"documentFreeViewDays": 3, "locale": "ru", "own": {}}
    # the nearest level wins; out of range → 422; clearing the locale drops the override
    r = c.put(f"{API}/units/{a['facility']}", json={"documentFreeViewDays": 1}, headers=h(ctx, "admin-a"))
    assert r.json()["settings"]["documentFreeViewDays"] == 1 and r.json()["settings"]["own"] == {"documentFreeViewDays": 1}
    assert c.put(f"{API}/units/{a['facility']}", json={"documentFreeViewDays": 91}, headers=h(ctx, "admin-a")).status_code == 422
    r = c.put(f"{API}/units/{a['region']}", json={"documentFreeViewDays": 0, "locale": None}, headers=h(ctx, "admin-a"))
    assert r.json()["settings"] == {"documentFreeViewDays": 0, "locale": None, "own": {"documentFreeViewDays": 0}}
    assert c.put(f"{API}/units/{a['facility']}", json={"documentFreeViewDays": 0}, headers=h(ctx, "admin-a")).status_code == 200


def test_sms_account_inheritance(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a = ctx["a"]
    assert c.get(f"{API}/units/{a['facility']}/sms", headers=h(ctx, "admin-a")).json()["mode"] == "inherited"
    # the region's own key: units below inherit it
    body = {"mode": "own", "apiKey": "xab_live_abcdef7f2a", "defaultPriority": "urgent", "senderNote": "note"}
    r = c.put(f"{API}/units/{a['region']}/sms", json=body, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json() == {"mode": "own", "provider": "xabarchi", "apiKeyMasked": "xab_live_••••••••7f2a", "defaultPriority": "urgent", "senderNote": "note", "effectiveUnitId": a["region"], "effectiveUnitName": f"T-{ctx['tag']} viloyati"}
    assert "abcdef" not in r.text
    fac = c.get(f"{API}/units/{a['facility']}/sms", headers=h(ctx, "admin-a")).json()
    assert fac["mode"] == "inherited" and fac["provider"] == "xabarchi" and fac["apiKeyMasked"] == "xab_live_••••••••7f2a" and fac["effectiveUnitId"] == a["region"] and fac["defaultPriority"] == "urgent"
    assert c.get(f"{API}/units/{a['facility']}", headers=h(ctx, "admin-a")).json()["sms"]["effectiveUnitId"] == a["region"]
    # keeps the key when apiKey is omitted; needs one the first time
    r = c.put(f"{API}/units/{a['region']}/sms", json={"mode": "own", "defaultPriority": "bulk"}, headers=h(ctx, "admin-a"))
    assert r.json()["apiKeyMasked"] == "xab_live_••••••••7f2a" and r.json()["defaultPriority"] == "bulk"
    r = c.put(f"{API}/units/{a['district']}/sms", json={"mode": "own"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_api_key_required"
    # the facility switches SMS off for itself only; the district still inherits the region's key
    r = c.put(f"{API}/units/{a['facility']}/sms", json={"mode": "off"}, headers=h(ctx, "admin-a"))
    assert r.json()["mode"] == "off" and r.json()["provider"] == "none" and r.json()["effectiveUnitId"] is None
    assert c.get(f"{API}/units/{a['district']}/sms", headers=h(ctx, "admin-a")).json()["provider"] == "xabarchi"

    async def accounts(s):
        return await messaging.sms_account_for(s, uuid.UUID(a["facility"])), await messaging.sms_account_for(s, uuid.UUID(a["district"]))

    off, district = db(c, accounts)
    assert off is None and district is not None and district.unit_id == uuid.UUID(a["region"]) and district.priority == "bulk"
    # back to inheriting; dropping the region's account switches everything below off
    assert c.put(f"{API}/units/{a['facility']}/sms", json={"mode": "inherited"}, headers=h(ctx, "admin-a")).json()["provider"] == "xabarchi"
    r = c.put(f"{API}/units/{a['region']}/sms", json={"mode": "inherited"}, headers=h(ctx, "admin-a"))
    assert r.json() == {"mode": "inherited", "provider": "none", "apiKeyMasked": None, "defaultPriority": "transactional", "senderNote": None, "effectiveUnitId": None, "effectiveUnitName": None}
    assert c.get(f"{API}/units/{a['facility']}/sms", headers=h(ctx, "admin-a")).json()["provider"] == "none"
    assert c.put(f"{API}/units/{a['facility']}/sms", json={"mode": "off"}, headers=h(ctx, "admin-b")).status_code == 403
    assert c.get(f"{API}/units/{a['facility']}/sms", headers=h(ctx, "admin-b")).status_code == 403


def test_sms_test(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    a = ctx["a"]
    r = c.post(f"{API}/units/{a['facility']}/sms/test", json={}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_not_configured"
    assert c.put(f"{API}/units/{a['region']}/sms", json={"mode": "own", "apiKey": "xab_live_testkey1234", "defaultPriority": "transactional"}, headers=h(ctx, "admin-a")).status_code == 200
    seen: dict[str, object] = {}

    async def fake_send(api_key: str, to: list[str], text: str, priority: str = "transactional") -> list[ProviderResult]:
        seen.update({"key": api_key, "to": to, "priority": priority, "text": text})
        return [ProviderResult(to=to[0], provider_id="777", status="queued", raw={})]

    monkeypatch.setattr(units_service.xabarchi, "send_sms", fake_send)
    # the facility sends with the inherited key
    r = c.post(f"{API}/units/{a['facility']}/sms/test", json={"to": "+998 90 765-43-21"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json() == {"ok": True, "providerMessageId": "777", "to": "998907654321"}
    assert seen["key"] == "xab_live_testkey1234" and seen["to"] == ["998907654321"] and seen["priority"] == "transactional"
    # default recipient: the unit's own phone (set in the update test), else the nearest ancestor's
    r = c.post(f"{API}/units/{a['facility']}/sms/test", json={}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and seen["to"] == ["998901112233"]
    assert c.put(f"{API}/units/{a['region']}", json={"phone": "998951112233"}, headers=h(ctx, "admin-a")).status_code == 200
    r = c.post(f"{API}/units/{a['district']}/sms/test", json={}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and seen["to"] == ["998951112233"]

    async def failing(*_: object, **__: object) -> list[ProviderResult]:
        raise XabarchiError("Xabarchi API kaliti rad etildi (401/403)", code="sms_auth_error")

    monkeypatch.setattr(units_service.xabarchi, "send_sms", failing)
    r = c.post(f"{API}/units/{a['facility']}/sms/test", json={}, headers=h(ctx, "admin-a"))
    assert r.status_code == 502 and r.json()["error"]["message"].startswith("Xabarchi API kaliti rad etildi")
    assert c.put(f"{API}/units/{a['region']}/sms", json={"mode": "inherited"}, headers=h(ctx, "admin-a")).status_code == 200


def test_sms_texts_inheritance(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a = ctx["a"]
    r = c.get(f"{API}/units/{a['facility']}/sms-templates", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json() == {"unitId": a["facility"], "templates": {"payment_receipt": None, "result_ready": None, "reminder": None}, "own": {"payment_receipt": None, "result_ready": None, "reminder": None}, "inherited": True, "applied": 0}
    # the region's texts: the facility inherits them
    r = c.put(f"{API}/units/{a['region']}/sms-templates", json={"templates": {"payment_receipt": "Chek {order}: {amount} — {unit}, {org}", "reminder": ""}}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json()["templates"] == {"payment_receipt": "Chek {order}: {amount} — {unit}, {org}", "result_ready": None, "reminder": None} and r.json()["inherited"] is False and r.json()["applied"] == 1
    fac = c.get(f"{API}/units/{a['facility']}/sms-templates", headers=h(ctx, "admin-a")).json()
    assert fac["inherited"] is True and fac["templates"]["payment_receipt"] == "Chek {order}: {amount} — {unit}, {org}" and fac["own"]["payment_receipt"] is None
    # the facility's own text wins for its key, the rest still comes from the region
    r = c.put(f"{API}/units/{a['facility']}/sms-templates", json={"templates": {"result_ready": "Filial: {service} {link}"}}, headers=h(ctx, "admin-a"))
    assert r.json()["inherited"] is False and r.json()["templates"] == {"payment_receipt": "Chek {order}: {amount} — {unit}, {org}", "result_ready": "Filial: {service} {link}", "reminder": None}
    assert c.get(f"{API}/units/{a['district']}/sms-templates", headers=h(ctx, "admin-a")).json()["templates"]["result_ready"] is None

    async def texts(s):
        fid = uuid.UUID(a["facility"])
        return (
            await messaging.payment_receipt_text(s, fid, "UR-9", 120000, total=150000, remaining=30000),
            await messaging.result_ready_text(s, fid, "Qon", link="L"),
            await messaging.result_ready_order_text(s, fid, "Virus", 2, link="L"),
            await messaging.result_ready_text(s, uuid.UUID(a["district"]), "Qon", link="L"),
        )

    receipt, ready, ready_order, district_ready = db(c, texts)
    # {unit} = the facility, {org} = the nearest unit above facility level (the district node here)
    assert receipt == f"Chek UR-9: 120\u00a0000 — T-{ctx['tag']} SES 2, T-{ctx['tag']} tumani"  # money grouped with NBSP
    assert ready == "Filial: Qon L" and ready_order == "Filial: Virus L"  # the order-scope text falls back to the unit's result text
    assert district_ready == f"Qon natijasi tayyor: L T-{ctx['tag']} tumani"
    # applyToAll writes the same texts on every unit of the subtree — they stop inheriting
    r = c.put(f"{API}/units/{a['region']}/sms-templates", json={"templates": {"payment_receipt": "Hammaga {order}"}, "applyToAll": True}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["applied"] >= 3
    fac = c.get(f"{API}/units/{a['facility']}/sms-templates", headers=h(ctx, "admin-a")).json()
    assert fac["inherited"] is False and fac["own"] == {"payment_receipt": "Hammaga {order}", "result_ready": None, "reminder": None}
    assert c.put(f"{API}/units/{a['facility']}/sms-templates", json={"templates": {}}, headers=h(ctx, "admin-b")).status_code == 403


def test_telegram_settings(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    a = ctx["a"]

    async def fake_get_me(token: str) -> str:
        assert token == "123456:ABC-DEF-token"
        return "t_units_bot"

    monkeypatch.setattr(units_service, "_telegram_get_me", fake_get_me)
    r = c.put(f"{API}/units/{a['region']}/telegram", json={"botToken": "123456:ABC-DEF-token"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json()["telegram"] == {"botUsername": "t_units_bot", "connected": True, "own": True, "effectiveUnitId": a["region"]}
    assert "ABC-DEF" not in r.text
    # the facility uses the region's bot
    fac = c.get(f"{API}/units/{a['facility']}", headers=h(ctx, "admin-a")).json()["telegram"]
    assert fac == {"botUsername": "t_units_bot", "connected": True, "own": False, "effectiveUnitId": a["region"]}
    status = c.get(f"{API}/units/{a['facility']}/telegram/status", headers=h(ctx, "admin-a"))
    assert status.status_code == 200, status.text
    assert status.json()["connected"] is True and status.json()["inherited"] is True and status.json()["running"] is False and status.json()["botUsername"] == "t_units_bot" and status.json()["effectiveUnitId"] == a["region"]
    assert status.json()["accountId"]
    assert c.get(f"{API}/units/{a['region']}/telegram/status", headers=h(ctx, "admin-a")).json()["inherited"] is False
    r = c.put(f"{API}/units/{a['region']}/telegram", json={"botToken": None}, headers=h(ctx, "admin-a"))
    assert r.json()["telegram"] == {"botUsername": None, "connected": False, "own": False, "effectiveUnitId": None}
    assert c.get(f"{API}/units/{a['facility']}/telegram/status", headers=h(ctx, "admin-a")).json() == {"connected": False, "running": False, "botUsername": None, "accountId": None, "inherited": False, "effectiveUnitId": None}
    assert c.put(f"{API}/units/{a['region']}/telegram", json={"botToken": "x"}, headers=h(ctx, "admin-b")).status_code == 403
    assert c.get(f"{API}/units/{a['region']}/telegram/status", headers=h(ctx, "admin-b")).status_code == 403


def test_superadmin_reset(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a = ctx["a"]
    ha = h(ctx, "admin-a")
    fac = a["facility"]
    # something to remove: a category, a service, a patient and a cheque at the facility
    cat = c.post(f"{API}/units/{fac}/categories", json={"name": f"T-{ctx['tag']} Lab"}, headers=ha).json()
    st = c.post(f"{API}/units/{fac}/service-types", json={"categoryId": cat["id"], "name": f"T-{ctx['tag']} Qon", "price": 50000}, headers=ha).json()
    patient = c.post(f"{API}/units/{fac}/patients", json={"fullName": f"T-{ctx['tag']} Bemor", "phone": phone()}, headers=ha)
    assert patient.status_code == 201, patient.text
    order = c.post(f"{API}/units/{fac}/orders", json={"patientId": patient.json()["id"], "serviceTypeIds": [st["id"]]}, headers=ha)
    assert order.status_code == 201, order.text
    assert order.json()["order"]["number"] == f"{ctx['code_a']}-000001"
    # superadmin only; the preview changes nothing
    r = c.get(f"{API}/units/{fac}/reset", headers=ha)
    assert r.status_code == 403 and r.json()["error"]["message"] == "Faqat superadmin uchun"
    r = c.get(f"{API}/units/{fac}/reset", headers=h(ctx, "super"))
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["id"] == fac and preview["level"] == "facility" and preview["confirmWord"] == ctx["code_a"]
    assert [p["key"] for p in preview["parts"]] == list(FACILITY_PARTS)
    parts = {p["key"]: p for p in preview["parts"]}
    assert parts["orders"]["counts"]["orders"] == 1 and parts["orders"]["counts"]["items"] == 1 and parts["orders"]["requires"] == []
    assert parts["patients"]["counts"]["patients"] == 1 and parts["patients"]["requires"] == ["orders"]
    assert parts["staff"]["requires"] == ["orders"] and parts["templates"]["requires"] == ["orders"]
    assert c.get(f"{API}/orders/{order.json()['order']['id']}", headers=ha).status_code == 200
    # the confirmation word is the facility code
    r = c.post(f"{API}/units/{fac}/reset", json={"parts": ["orders"], "confirm": "nope"}, headers=h(ctx, "super"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "confirm"
    r = c.post(f"{API}/units/{fac}/reset", json={"parts": ["units"], "confirm": ctx["code_a"]}, headers=h(ctx, "super"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "parts"
    r = c.post(f"{API}/units/{fac}/reset", json={"parts": ["patients"], "confirm": ctx["code_a"].lower()}, headers=h(ctx, "super"))
    assert r.status_code == 200, r.text
    assert r.json()["parts"] == ["orders", "patients"] and r.json()["counts"]["orders"] == 1 and r.json()["counts"]["patients"] == 1
    # gone: the cheque and the patient; the numbering restarts; the catalogue stays
    assert c.get(f"{API}/orders/{order.json()['order']['id']}", headers=ha).status_code == 404
    assert c.get(f"{API}/patients/{patient.json()['id']}", headers=ha).status_code == 404
    assert c.get(f"{API}/service-types/{st['id']}", headers=ha).status_code == 200
    p2 = c.post(f"{API}/units/{fac}/patients", json={"fullName": f"T-{ctx['tag']} Bemor 2", "phone": phone()}, headers=ha).json()
    again = c.post(f"{API}/units/{fac}/orders", json={"patientId": p2["id"], "serviceTypeIds": [st["id"]]}, headers=ha)
    assert again.status_code == 201 and again.json()["order"]["number"] == f"{ctx['code_a']}-000001"
    # a unit reset offers the unit parts and is confirmed by the unit's name
    r = c.get(f"{API}/units/{a['district']}/reset", headers=h(ctx, "super")).json()
    assert r["confirmWord"] == f"T-{ctx['tag']} tumani" and [p["key"] for p in r["parts"]] == ["orders", "patients", "templates", "catalog", "staff", "units"]
    assert {p["key"]: p["requires"] for p in r["parts"]}["catalog"] == ["templates", "orders"]
