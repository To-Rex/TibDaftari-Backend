"""The assistant as an operator of the whole role: it is told where the employee works (company → … → facility),
asks with buttons instead of guessing, shows a proposed write with the names of what it touches, refuses a unit
outside the territory before anything about it reaches a card, makes the employee type the word of an irreversible
write, and adds a sample facility's working set to another facility without duplicating it. The model is a
scripted fake (see `test_assistant`)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from app.core.crypto import encrypt
from app.infrastructure.db.models import AssistantAccount, OrgUnit
from app.modules.assistant import service
from app.modules.assistant import tools as tl
from fastapi.testclient import TestClient

from tests.helpers import API, app_instance, db, make_employee, make_facility, make_tree, seed, staff_token
from tests.test_assistant import FakeProvider, h, new_thread, send

LEVELS = ["company", "country", "region", "district", "facility"]


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        a2 = await make_facility(s, a.district, name=f"T-{a.tag} Poliklinika", kind="polyclinic", code=f"P{a.tag}")
        for t in (a, b):
            s.add(AssistantAccount(unit_id=t.region.id, provider="openai", api_key_enc=encrypt("sk-test"), api_key_masked="sk-t••••test", settings={}))
        sup = await make_employee(s, None, super_admin=True)
        admin_a = await make_employee(s, a.region, "admin")
        reg = await make_employee(s, a.facility, "registrator")
        await s.flush()
        return {
            "a": {k: str(getattr(a, k).id) for k in ("region", "district", "facility")} | {"facility2": str(a2.id), "company": a.company.name, "tag": a.tag},
            "b": {k: str(getattr(b, k).id) for k in ("region", "district", "facility")} | {"tag": b.tag, "code": b.facility.code, "name": b.facility.name},
            "reg_id": str(reg.id),
            "logins": {"super": sup.login, "admin-a": admin_a.login, "reg": reg.login},
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, login) for who, login in data["logins"].items()}
        yield data


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    f = FakeProvider()
    monkeypatch.setattr(service, "build_provider", lambda cfg: f)
    monkeypatch.setattr(service, "RATE_LIMIT", 10_000)
    return f


def texts(out: dict[str, Any]) -> str:
    return " ".join(m["content"] for m in out["messages"])


def test_the_assistant_is_told_where_the_employee_works(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    a, tag = ctx["a"], ctx["a"]["tag"]
    # the same lineage goes to the employee (status) and to the model (the prompt)
    st = c.get(f"{API}/assistant/status", params={"unitId": a["facility"]}, headers=h(ctx, "admin-a")).json()
    assert [x["level"] for x in st["context"]] == LEVELS and st["context"][-1]["name"] == f"T-{tag} SES" and st["context"][0]["name"] == a["company"]
    assert {t["group"] for t in st["tools"]} >= {"units", "staff", "catalog", "templates", "reports"} and all(t["name"] != tl.ASK_TOOL for t in st["tools"])
    assert next(t for t in st["tools"] if t["name"] == "delete_template")["danger"] is True

    tid = new_thread(ctx, "admin-a", a["facility"])
    fake.say("Tayyor.")
    send(ctx, "admin-a", tid, "Men qayerdaman?", unit_id=a["facility"])
    system = fake.calls[-1]["system"]
    for line in (f"- Kompaniya: {a['company']}", f"- Viloyat: T-{tag} viloyati", f"- Tuman / shahar: T-{tag} tumani", f"- Muassasa: T-{tag} SES (kod T{tag}) [id {a['facility']}]", "Ish birligi darajasi: muassasa", "TDK-"):
        assert line in system, line
    assert "copy_working_set*" in system and "ask_user" in system and "reset_unit" not in system  # an admin has no reset right

    # one level up: what lies below, so "the facility" can be resolved or asked about
    fake.say("Tayyor.")
    send(ctx, "admin-a", tid, "Endi viloyatda", unit_id=a["region"])
    system = fake.calls[-1]["system"]
    assert "Ish birligi darajasi: viloyat" in system and "1 tuman, 2 muassasa" in system and "- Muassasa:" not in system

    # a registrar's assistant knows only the reception's tools
    rid = new_thread(ctx, "reg", a["facility"])
    fake.say("Salom.")
    send(ctx, "reg", rid, "Salom")
    offered = set(fake.calls[-1]["tools"])
    assert {"create_patient", "create_order", "pay_order", "ask_user"} <= offered and not ({"update_unit", "create_role", "copy_working_set", "approve_result", "refund_payment"} & offered)


def test_a_question_with_buttons_waits_for_the_employee(ctx: dict, fake: FakeProvider) -> None:
    tag = ctx["a"]["tag"]
    tid = new_thread(ctx, "admin-a", ctx["a"]["district"])
    options = [f"T-{tag} SES", f"T-{tag} Poliklinika"]
    fake.call("ask_user", {"question": "Qaysi muassasaga qo‘shay?", "options": options}, text="Tumanda ikkita muassasa bor.")
    fake.say("Xo‘p, SES tanlandi.")
    out = send(ctx, "admin-a", tid, "Muassasaga andozalarni qo‘shib ber")
    last = out["messages"][-1]
    assert last["choices"] == {"question": "Qaysi muassasaga qo‘shay?", "options": options} and last["toolCalls"] == [] and out["pending"] is None
    assert len(fake.script) == 1  # the loop stopped at the question — nothing was decided for the employee
    out = send(ctx, "admin-a", tid, options[0])
    turns = fake.calls[-1]["turns"]
    assert [t.role for t in turns[-3:]] == ["assistant", "tool", "user"] and turns[-1].content == options[0]
    assert out["messages"][-1]["content"] == "Xo‘p, SES tanlandi." and out["messages"][-1]["choices"] is None
    # one option is not a choice — the model hears it instead of a dead end on the screen
    fake.call("ask_user", {"question": "?", "options": ["yagona"]})
    fake.say("Bitta variant — shuni olaman.")
    out = send(ctx, "admin-a", tid, "yana")
    assert all(m["choices"] is None for m in out["messages"]) and "Kamida ikkita" in fake.calls[-1]["turns"][-1].content


def test_a_proposed_write_names_what_it_touches_and_stays_inside_the_territory(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    a, b, tag = ctx["a"], ctx["b"], ctx["a"]["tag"]
    tid = new_thread(ctx, "admin-a", a["facility"])

    # a field the tool does not offer never becomes a card — the model is told
    fake.call("update_employee", {"employeeId": ctx["reg_id"], "isSuperAdmin": True})
    fake.say("Bu huquq chat orqali berilmaydi.")
    out = send(ctx, "admin-a", tid, "Registratorni superadmin qil")
    assert out["pending"] is None and "unknown_argument" in fake.calls[-1]["turns"][-1].content

    # somebody else's unit: refused before its name could reach a card or a reply
    fake.call("update_unit", {"unitId": b["facility"], "name": "Boshqa nom"})
    fake.say("Bu birlik sizning hududingizda emas.")
    out = send(ctx, "admin-a", tid, "Narigi viloyat muassasasini qayta nomla")
    assert out["pending"] is None and b["name"] not in texts(out) and b["name"] not in fake.calls[-1]["turns"][-1].content

    # a real one: the card says which unit, by name and path; the server's notes never show as arguments
    fake.call("update_unit", {"unitId": a["facility2"], "phone": "+998 62 111-22-33"}, text="Telefonni o‘zgartiraman.")
    out = send(ctx, "admin-a", tid, "Poliklinika telefonini o‘zgartir")
    act = out["pending"]
    assert act and act["danger"] is False and act["confirmWord"] is None and tl.META_KEY not in act["args"]
    assert act["details"][0]["label"] == "Birlik" and f"T-{tag} tumani" in act["details"][0]["value"] and act["details"][0]["value"].endswith(f"T-{tag} Poliklinika")
    assert {"label": "Telefon", "value": "+998 62 111-22-33"} in act["details"]
    fake.say("Yangilandi.")
    done = c.post(f"{API}/assistant/actions/{act['id']}/confirm", headers=h(ctx, "admin-a"))
    assert done.status_code == 200, done.text
    assert db(c, lambda s: s.get(OrgUnit, a["facility2"])).phone == "+998 62 111-22-33"

    # no unit named → the working unit, and the card says so before the yes
    fake.call("create_role", {"name": f"Kassa yordamchisi {tag}", "permissions": ["reception.patient.read"]})
    out = send(ctx, "admin-a", tid, "Yangi rol och")
    act = out["pending"]
    assert act["details"][0] == {"label": "Birlik", "value": act["details"][0]["value"]} and act["details"][0]["value"].endswith(f"T-{tag} SES")
    fake.say("Bekor qilindi.")
    assert c.post(f"{API}/assistant/actions/{act['id']}/cancel", headers=h(ctx, "admin-a")).status_code == 200


def test_an_irreversible_write_needs_the_word_typed_by_the_employee(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    b = ctx["b"]
    tid = new_thread(ctx, "super", b["region"])
    fake.call("reset_unit", {"unitId": b["facility"], "parts": ["orders"]}, text="Cheklarni tozalayman — bu qaytarilmaydi.")
    out = send(ctx, "super", tid, f"{b['name']} cheklarini tozala", unit_id=b["region"])
    act = out["pending"]
    assert act["danger"] is True and act["confirmWord"] == b["code"] and "confirm" not in act["args"]
    url = f"{API}/assistant/actions/{act['id']}/confirm"
    # a click is not enough, and neither is a wrong word — the action stays pending
    for body in (None, {"confirm": "ha"}):
        r = c.post(url, json=body, headers=h(ctx, "super"))
        assert r.status_code == 422 and r.json()["error"]["code"] == "confirm_mismatch", r.text
    assert c.get(f"{API}/assistant/threads/{tid}", headers=h(ctx, "super")).json()["pending"]["id"] == act["id"]
    fake.say("Tozalandi.")
    r = c.post(url, json={"confirm": f"  {b['code'].lower()} "}, headers=h(ctx, "super"))
    assert r.status_code == 200, r.text
    resolved = next(m["action"] for m in c.get(f"{API}/assistant/threads/{tid}", headers=h(ctx, "super")).json()["messages"] if m["action"])
    assert resolved["status"] == "done" and resolved["confirmWord"] is None and resolved["danger"] is True


def test_a_sample_working_set_is_added_once(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    a, tag = ctx["a"], ctx["a"]["tag"]
    hd = h(ctx, "admin-a")
    # the sample facility works with a category, a service and a cheque of its own
    cat = c.post(f"{API}/units/{a['facility']}/categories", json={"name": f"Bakteriologiya {tag}"}, headers=hd)
    assert cat.status_code == 201, cat.text
    svc = c.post(f"{API}/units/{a['facility']}/service-types", json={"categoryId": cat.json()["id"], "name": f"Suv tahlili {tag}", "price": 50_000}, headers=hd)
    assert svc.status_code == 201, svc.text
    assert c.post(f"{API}/units/{a['facility']}/templates/default-receipt", json={"name": f"Chek {tag}"}, headers=hd).status_code == 201

    tid = new_thread(ctx, "admin-a", a["district"])
    fake.call("list_sample_facilities", {"facilityId": a["facility2"]})
    fake.call("copy_working_set", {"facilityId": a["facility2"], "sampleFacilityId": a["facility"]}, text="Namunadan ko‘chiraman.")
    out = send(ctx, "admin-a", tid, "Poliklinikaga SES andozalarini qo‘shib ber")
    assert f"T-{tag} SES" in fake.calls[-1]["turns"][-1].content  # the sample was found by the read tool
    act = out["pending"]
    labels = {d["label"]: d["value"] for d in act["details"]}
    assert labels["Muassasa"].endswith(f"T-{tag} Poliklinika") and labels["Namuna muassasa"].endswith(f"T-{tag} SES")
    fake.say("Ko‘chirildi.")
    r = c.post(f"{API}/assistant/actions/{act['id']}/confirm", headers=hd)
    assert r.status_code == 200, r.text
    result = fake.calls[-1]["turns"][-1].content
    assert '"categories": 1' in result and '"serviceTypes": 1' in result and '"receipt": {' in result
    mine = c.get(f"{API}/units/{a['facility2']}/service-types", params={"unitId": a["facility2"]}, headers=hd).json()
    assert [s["name"] for s in mine if s["ownerUnitId"] == a["facility2"]] == [f"Suv tahlili {tag}"]

    # asked again: nothing is copied twice
    fake.call("copy_working_set", {"facilityId": a["facility2"], "sampleFacilityId": a["facility"]})
    out = send(ctx, "admin-a", tid, "Yana bir marta")
    fake.say("Hammasi allaqachon bor.")
    assert c.post(f"{API}/assistant/actions/{out['pending']['id']}/confirm", headers=hd).status_code == 200
    again = fake.calls[-1]["turns"][-1].content
    assert '"copied": {"categories": 0, "serviceTypes": 0' in again
    mine = c.get(f"{API}/units/{a['facility2']}/service-types", params={"unitId": a["facility2"]}, headers=hd).json()
    assert len([s for s in mine if s["ownerUnitId"] == a["facility2"]]) == 1
