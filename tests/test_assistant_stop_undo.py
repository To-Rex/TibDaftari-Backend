"""Stopping the assistant and taking its writes back. A stop drops the model call at once and leaves the
conversation whole; an undo runs the kept plan through the assistant's own tools with the employee's rights, puts
back only what still holds the assistant's values, and needs no model at all. The model is a scripted fake."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.crypto import encrypt
from app.infrastructure.db.models import AssistantAccount, OrgUnit
from app.modules.assistant import service
from app.modules.assistant import tools as tl
from app.modules.assistant import undo as ud
from app.modules.assistant.providers import Reply, ToolCall
from fastapi.testclient import TestClient

from tests.helpers import API, app_instance, db, make_employee, make_facility, make_tree, seed, staff_token
from tests.test_assistant import FakeProvider, h, new_thread, send


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        a2 = await make_facility(s, a.district, name=f"T-{a.tag} Poliklinika", kind="polyclinic", code=f"P{a.tag}", phone="+998 62 100-00-00")
        s.add(AssistantAccount(unit_id=a.region.id, provider="openai", api_key_enc=encrypt("sk-test"), api_key_masked="sk-t••••test", settings={}))
        admin = await make_employee(s, a.region, "admin")
        other = await make_employee(s, a.region, "admin")
        await s.flush()
        return {"tag": a.tag, "region": str(a.region.id), "facility": str(a.facility.id), "facility2": str(a2.id), "logins": {"admin": admin.login, "other": other.login}}

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


def propose_and_confirm(ctx: dict, fake: FakeProvider, tid: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """The model proposes a write, the employee says yes — the done action as the thread shows it."""
    c: TestClient = ctx["client"]
    fake.call(tool, args)
    act = send(ctx, "admin", tid, f"{tool} qil")["pending"]
    assert act is not None, tool
    fake.say("Bajarildi.")
    r = c.post(f"{API}/assistant/actions/{act['id']}/confirm", headers=h(ctx, "admin"))
    assert r.status_code == 200, r.text
    return next(m["action"] for m in c.get(f"{API}/assistant/threads/{tid}", headers=h(ctx, "admin")).json()["messages"] if m["action"] and m["action"]["id"] == act["id"])


def unit(ctx: dict, uid: str) -> OrgUnit:
    return db(ctx["client"], lambda s: s.get(OrgUnit, uid))


def test_recipes_are_made_of_the_assistants_own_tools() -> None:
    assert {"update_unit", "update_employee", "update_patient", "create_order", "pay_order", "save_result_values", "approve_result", "create_role"} <= set(ud.RECIPES)
    # what cannot honestly be taken back has no recipe
    assert not ({"send_sms", "refund_payment", "cancel_order", "delete_order", "delete_template", "reset_unit", "quick_start", "copy_working_set", "publish_schema", "resend_result_sms"} & set(ud.RECIPES))
    for name, recipe in ud.RECIPES.items():
        assert tl.REGISTRY[name].kind == "write" and recipe.label, name
    assert ud._same(None, "") and ud._same(["a", "b"], ["b", "a"]) and not ud._same("a", "b") and not ud._same(0, None) and ud._same(False, False)


def test_a_stop_drops_the_model_call_and_keeps_the_conversation_whole(ctx: dict, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    tid = new_thread(ctx, "admin", ctx["facility"])
    url = f"{API}/assistant/threads/{tid}/stop"
    # nothing runs → nothing to stop; somebody else's thread is not theirs to stop
    assert c.post(url, headers=h(ctx, "admin")).json() == {"stopping": False}
    assert c.post(url, headers=h(ctx, "other")).status_code == 404

    class Slow(FakeProvider):
        """The employee presses stop while the model is still thinking."""

        async def complete(self, **kw: Any) -> Reply:
            self.calls.append(kw)
            assert await service.request_stop(tid)  # what POST …/stop does
            await asyncio.sleep(60)
            raise AssertionError("the model call was not cancelled")

    slow = Slow()
    monkeypatch.setattr(service, "build_provider", lambda cfg: slow)
    started = time.monotonic()
    out = send(ctx, "admin", tid, "Uzoq hisobot tayyorla")
    assert time.monotonic() - started < 30  # the minute of "thinking" was not waited for
    assert [m["role"] for m in out["messages"]] == ["user", "assistant"]
    assert out["messages"][-1]["stopped"] is True and out["messages"][-1]["content"] == service.STOP_NOTE and out["pending"] is None
    assert c.post(url, headers=h(ctx, "admin")).json() == {"stopping": False}  # the run is over, its mark is gone

    class Halfway(FakeProvider):
        """The model asked for two tools; the stop lands before the first one starts."""

        async def complete(self, **kw: Any) -> Reply:
            self.calls.append(kw)
            assert await service.request_stop(tid)
            return Reply(text="Ikki narsani tekshiraman.", tool_calls=[ToolCall(id="c1", name="whoami", args={}), ToolCall(id="c2", name="update_unit", args={"unitId": ctx["facility2"], "name": "Boshqa"})], stop="tool", usage={})

    monkeypatch.setattr(service, "build_provider", lambda cfg: Halfway())
    out = send(ctx, "admin", tid, "Yana")
    assert out["pending"] is None and out["messages"][-1]["stopped"] is True  # no tool ran, no write was proposed
    assert unit(ctx, ctx["facility2"]).name == f"T-{ctx['tag']} Poliklinika"

    # the next message finds a whole conversation: every tool call has its answer, the stops are part of the story
    monkeypatch.setattr(service, "build_provider", lambda cfg: fake)
    fake.say("Davom etamiz.")
    out = send(ctx, "admin", tid, "Davom et")
    turns = fake.calls[-1]["turns"]
    asked = [c.id for t in turns if t.role == "assistant" for c in t.tool_calls]
    answered = [t.tool_call_id for t in turns if t.role == "tool"]
    assert asked == answered == ["c1", "c2"] and sum(t.content == service.STOP_NOTE for t in turns) == 2
    assert out["messages"][-1]["content"] == "Davom etamiz." and out["messages"][-1]["stopped"] is False


def test_an_update_is_taken_back_only_where_nobody_changed_it_since(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    f2, tag = ctx["facility2"], ctx["tag"]
    tid = new_thread(ctx, "admin", ctx["facility"])
    st = c.get(f"{API}/assistant/status", params={"unitId": ctx["facility"]}, headers=h(ctx, "admin")).json()
    flags = {t["name"]: t["undoable"] for t in st["tools"]}
    assert flags["update_unit"] and flags["create_role"] and not flags["send_sms"] and not flags["delete_template"]

    # a plain undo: both fields go back, the thread says so, no model is asked
    act = propose_and_confirm(ctx, fake, tid, "update_unit", {"unitId": f2, "name": f"T-{tag} Yangi nom", "phone": "+998 62 999-99-99"})
    assert act["undoable"] and act["undo"]["available"] and act["undo"]["label"] and unit(ctx, f2).name == f"T-{tag} Yangi nom"
    asked = len(fake.calls)
    r = c.post(f"{API}/assistant/actions/{act['id']}/undo", headers=h(ctx, "admin"))
    assert r.status_code == 200, r.text
    origin, note = r.json()["messages"]
    assert origin["action"]["undo"]["available"] is False and origin["action"]["undo"]["undoneAt"] and origin["action"]["status"] == "done"
    assert note["undoOf"] == act["id"] and "orqaga qaytarildi" in note["content"] and "Nomi" in note["content"] and "Telefon" in note["content"]
    back = unit(ctx, f2)
    assert back.name == f"T-{tag} Poliklinika" and back.phone == "+998 62 100-00-00" and len(fake.calls) == asked
    assert c.post(f"{API}/assistant/actions/{act['id']}/undo", headers=h(ctx, "admin")).json()["error"]["code"] == "undo_done"
    assert c.post(f"{API}/assistant/actions/{act['id']}/undo", headers=h(ctx, "other")).status_code == 404

    # a person changed one of the two fields afterwards: that one stays theirs, the other goes back
    act = propose_and_confirm(ctx, fake, tid, "update_unit", {"unitId": f2, "name": f"T-{tag} Ikkinchi nom", "phone": "+998 62 888-88-88"})
    assert c.put(f"{API}/units/{f2}", json={"phone": "+998 62 777-77-77"}, headers=h(ctx, "other")).status_code == 200
    r = c.post(f"{API}/assistant/actions/{act['id']}/undo", headers=h(ctx, "admin"))
    assert r.status_code == 200, r.text
    assert "tegilmadi: Telefon" in r.json()["messages"][-1]["content"]
    back = unit(ctx, f2)
    assert back.name == f"T-{tag} Poliklinika" and back.phone == "+998 62 777-77-77"

    # everything the write set was changed since: nothing is overwritten, the way back stays refused
    act = propose_and_confirm(ctx, fake, tid, "update_unit", {"unitId": f2, "address": "Yangi ko‘cha, 1"})
    assert c.put(f"{API}/units/{f2}", json={"address": "Eski ko‘cha, 7"}, headers=h(ctx, "other")).status_code == 200
    r = c.post(f"{API}/assistant/actions/{act['id']}/undo", headers=h(ctx, "admin"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "undo_conflict" and unit(ctx, f2).address == "Eski ko‘cha, 7"


def test_what_was_created_is_removed_and_what_has_no_way_back_says_so(ctx: dict, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    tid = new_thread(ctx, "admin", ctx["facility"])
    name = f"Vaqtinchalik rol {ctx['tag']}"
    act = propose_and_confirm(ctx, fake, tid, "create_role", {"name": name, "permissions": ["reception.patient.read"]})
    roles = lambda: [r["name"] for r in c.get(f"{API}/units/{ctx['facility']}/roles", headers=h(ctx, "admin")).json()]  # noqa: E731
    assert name in roles() and act["undo"]["available"]
    assert c.post(f"{API}/assistant/actions/{act['id']}/undo", headers=h(ctx, "admin")).status_code == 200
    assert name not in roles()

    # a deletion has no recipe: the card knows before the yes, the route refuses after
    act = propose_and_confirm(ctx, fake, tid, "create_role", {"name": name + " 2", "permissions": []})
    rid = next(r["id"] for r in c.get(f"{API}/units/{ctx['facility']}/roles", headers=h(ctx, "admin")).json() if r["name"] == name + " 2")
    gone = propose_and_confirm(ctx, fake, tid, "delete_role", {"roleId": rid})
    assert gone["undoable"] is False and gone["undo"] is None and gone["danger"] is True
    assert c.post(f"{API}/assistant/actions/{gone['id']}/undo", headers=h(ctx, "admin")).json()["error"]["code"] == "undo_unavailable"
    # …and so the creation cannot be "taken back" a second way either: the role it would delete is already gone
    r = c.post(f"{API}/assistant/actions/{act['id']}/undo", headers=h(ctx, "admin"))
    assert r.status_code in (404, 409), r.text

    # the window closes
    act = propose_and_confirm(ctx, fake, tid, "update_unit", {"unitId": ctx["facility2"], "shortName": "PK"})
    monkeypatch.setattr(service, "UNDO_TTL", service.timedelta(seconds=-1))
    assert c.post(f"{API}/assistant/actions/{act['id']}/undo", headers=h(ctx, "admin")).json()["error"]["code"] == "undo_expired"
    detail = c.get(f"{API}/assistant/threads/{tid}", headers=h(ctx, "admin")).json()
    assert next(m["action"] for m in detail["messages"] if m["action"] and m["action"]["id"] == act["id"])["undo"]["available"] is False
