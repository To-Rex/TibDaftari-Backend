"""Several writes, one answer. A turn of the model may propose many independent writes: they wait together on one
card, the employee says yes to all or to some with ONE request, each runs in its own savepoint (one that fails
leaves nothing and does not stop the others), and the done ones can be taken back together. The model is a
scripted fake."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from app.core.crypto import encrypt
from app.infrastructure.db.models import AssistantAccount, OrgUnit
from app.modules.assistant import service
from app.modules.assistant.providers import Reply, ToolCall, Turn
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
        sup = await make_employee(s, None, super_admin=True)
        await s.flush()
        return {"tag": a.tag, "region": str(a.region.id), "facility": str(a.facility.id), "facility2": str(a2.id), "logins": {"admin": admin.login, "other": other.login, "super": sup.login}}

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


def many(fake: FakeProvider, *calls: tuple[str, dict[str, Any]], text: str = "") -> list[str]:
    """The model asks for several tools in ONE turn → the ids of the calls."""
    made = [ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=name, args=args) for name, args in calls]
    fake.script.append(Reply(text=text, tool_calls=made, stop="tool", usage={"input": 1, "output": 1}))
    return [c.id for c in made]


def unit(ctx: dict, uid: str) -> OrgUnit:
    return db(ctx["client"], lambda s: s.get(OrgUnit, uid))


def roles(ctx: dict) -> list[str]:
    return [r["name"] for r in ctx["client"].get(f"{API}/units/{ctx['facility']}/roles", headers=h(ctx, "admin")).json()]


def card(ctx: dict, tid: str, who: str = "admin") -> list[dict[str, Any]]:
    """The actions of the last turn that proposed any, as the thread shows them."""
    detail = ctx["client"].get(f"{API}/assistant/threads/{tid}", headers=h(ctx, who)).json()
    return next(m["actions"] for m in reversed(detail["messages"]) if m["actions"])


def batch(ctx: dict, who: str, confirm: list[str], cancel: list[str] | None = None):
    return ctx["client"].post(f"{API}/assistant/actions/batch", json={"confirm": confirm, "cancel": cancel or []}, headers=h(ctx, who))


def answered(fake: FakeProvider) -> tuple[list[str], list[str]]:
    """(the tool calls the model has made, the ones it has an answer for) in the history it was last sent."""
    turns = fake.calls[-1]["turns"]
    return [c.id for t in turns if t.role == "assistant" for c in t.tool_calls], [t.tool_call_id for t in turns if t.role == "tool"]


def test_the_history_sent_to_the_model_is_always_whole() -> None:
    call = lambda cid: ToolCall(id=cid, name="whoami", args={})  # noqa: E731
    turns = [
        Turn(role="user", content="a"),
        Turn(role="assistant", content="", tool_calls=[call("c1"), call("c2")]),
        Turn(role="tool", content="{}", tool_call_id="c2"),
        Turn(role="user", content="b"),
        Turn(role="tool", content="{}", tool_call_id="c1"),  # an answer that came after the conversation moved on
        Turn(role="assistant", content="", tool_calls=[call("c3")]),
    ]
    out = service._whole(turns)
    assert [(t.role, t.tool_call_id) for t in out] == [("user", None), ("assistant", None), ("tool", "c2"), ("tool", "c1"), ("user", None), ("assistant", None), ("tool", "c3")]
    assert out[3].is_error and out[3].content == service.NO_RESULT and out[-1].is_error
    # a whole history passes through untouched
    whole = [Turn(role="user", content="a"), Turn(role="assistant", content="", tool_calls=[call("k")]), Turn(role="tool", content="{}", tool_call_id="k"), Turn(role="assistant", content="ok")]
    assert service._whole(whole) == whole


def test_several_writes_of_one_turn_wait_together_and_run_on_one_yes(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    fac, fac2, tag = ctx["facility"], ctx["facility2"], ctx["tag"]
    tid = new_thread(ctx, "admin", ctx["region"])
    role = f"T-{tag} navbatchi"
    ids = many(fake, ("update_unit", {"unitId": fac, "phone": "+998 62 111-11-11"}), ("update_unit", {"unitId": fac2, "shortName": "PK-1"}), ("create_role", {"name": role, "permissions": ["reception.patient.read"]}), text="Uchta o‘zgartirish taklif qilaman.")
    out = send(ctx, "admin", tid, "telefonni yangila, qisqa nom qo‘y va rol och")
    turn = out["messages"][1]
    acts = turn["actions"]
    # one turn, three writes waiting together — in the order the model asked for them; nothing has changed yet
    assert [a["tool"] for a in acts] == ["update_unit", "update_unit", "create_role"] and all(a["status"] == "pending" for a in acts)
    assert turn["action"]["id"] == acts[0]["id"] == out["pending"]["id"] and len({a["expiresAt"] for a in acts}) == 1
    assert [t["status"] for t in turn["toolCalls"]] == ["pending"] * 3 and [t["id"] for t in turn["toolCalls"]] == ids
    assert any(d["label"] == "Birlik" and f"T-{tag}" in d["value"] for d in acts[1]["details"]) and all(a["undoable"] for a in acts)
    assert unit(ctx, fac).phone != "+998 62 111-11-11" and role not in roles(ctx)

    # nobody else answers it; an empty answer is no answer
    assert batch(ctx, "other", [a["id"] for a in acts]).status_code == 404
    assert batch(ctx, "admin", []).json()["error"]["code"] == "batch_empty"

    # yes to the first and the third, no to the second — ONE request, one reply of the model
    fake.say("Ikkitasi bajarildi, bittasi bekor qilindi.")
    asked = len(fake.calls)
    r = batch(ctx, "admin", [acts[0]["id"], acts[2]["id"]], [acts[1]["id"]])
    assert r.status_code == 200, r.text
    done = r.json()
    assert len(fake.calls) == asked + 1 and done["pending"] is None and done["messages"][-1]["content"] == "Ikkitasi bajarildi, bittasi bekor qilindi."
    assert unit(ctx, fac).phone == "+998 62 111-11-11" and unit(ctx, fac2).short_name != "PK-1" and role in roles(ctx)
    # the model read an answer to every call: two results and one "declined"
    made, got = answered(fake)
    assert set(ids) <= set(made) and sorted(got) == sorted(made)
    declined = next(t for t in fake.calls[-1]["turns"] if t.role == "tool" and t.tool_call_id == ids[1])
    assert declined.is_error and "bekor" in declined.content.lower()
    # the card shows each outcome; an answered write is its card, not a red line under it
    after = card(ctx, tid)
    assert [a["status"] for a in after] == ["done", "cancelled", "done"] and after[0]["undo"]["available"] and after[2]["undo"]["available"] and after[1]["undo"] is None
    detail = c.get(f"{API}/assistant/threads/{tid}", headers=h(ctx, "admin")).json()
    assert not [m for m in detail["messages"] if m["role"] == "tool"] and detail["pending"] is None
    assert batch(ctx, "admin", [acts[0]["id"]]).json()["error"]["code"] == "action_resolved"


def test_a_write_that_fails_leaves_nothing_and_does_not_stop_the_others(ctx: dict, fake: FakeProvider) -> None:
    fac, fac2, tag = ctx["facility"], ctx["facility2"], ctx["tag"]
    tid = new_thread(ctx, "admin", ctx["region"])
    ghost = str(uuid.uuid4())
    ids = many(fake, ("update_unit", {"unitId": fac, "address": f"T-{tag} ko‘chasi, 1"}), ("update_employee", {"employeeId": ghost, "fullName": "T-yo‘q odam"}), ("update_unit", {"unitId": fac2, "address": f"T-{tag} ko‘chasi, 2"}))
    acts = send(ctx, "admin", tid, "uchta narsani o‘zgartir")["messages"][1]["actions"]
    assert len(acts) == 3
    fake.say("Ikkitasi bajarildi, xodim topilmadi.")
    r = batch(ctx, "admin", [a["id"] for a in acts])
    assert r.status_code == 200, r.text
    after = card(ctx, tid)
    assert [a["status"] for a in after] == ["done", "failed", "done"] and after[1]["error"] and after[1]["undo"] is None
    assert unit(ctx, fac).address == f"T-{tag} ko‘chasi, 1" and unit(ctx, fac2).address == f"T-{tag} ko‘chasi, 2"
    failed = next(t for t in fake.calls[-1]["turns"] if t.role == "tool" and t.tool_call_id == ids[1])
    assert failed.is_error and sorted(answered(fake)[0]) == sorted(answered(fake)[1])


def test_the_ceiling_the_lone_yes_and_the_word_that_must_be_typed(ctx: dict, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    fac, fac2 = ctx["facility"], ctx["facility2"]
    tid = new_thread(ctx, "admin", ctx["region"])
    # more writes than one turn may carry: the extra one is refused to the model, the rest wait
    monkeypatch.setattr(service, "MAX_BATCH", 2)
    ids = many(fake, ("update_unit", {"unitId": fac, "email": "a@t.uz"}), ("update_unit", {"unitId": fac2, "email": "b@t.uz"}), ("update_unit", {"unitId": fac2, "legalName": "T-uchinchi"}))
    out = send(ctx, "admin", tid, "uchtasini ham")
    acts = out["messages"][1]["actions"]
    assert len(acts) == 2 and out["messages"][1]["toolCalls"][2]["status"] == "error"
    monkeypatch.setattr(service, "MAX_BATCH", 30)

    # a client that knows one write per turn says yes to the first: that one runs, the other is declined — never left hanging
    fake.say("Birinchisi bajarildi.")
    r = c.post(f"{API}/assistant/actions/{acts[0]['id']}/confirm", headers=h(ctx, "admin"))
    assert r.status_code == 200, r.text
    assert [a["status"] for a in card(ctx, tid)] == ["done", "cancelled"] and unit(ctx, fac).email == "a@t.uz" and unit(ctx, fac2).email != "b@t.uz"
    made, got = answered(fake)
    assert set(ids) <= set(made) and sorted(made) == sorted(got)
    full = next(t for t in fake.calls[-1]["turns"] if t.role == "tool" and t.tool_call_id == ids[2])
    assert "batch_full" in full.content

    # a write that needs a typed word never rides in a batch: proposed with others it is refused to the model…
    sid = new_thread(ctx, "super", fac2)
    many(fake, ("reset_unit", {"unitId": fac2, "parts": ["orders"]}), ("update_unit", {"unitId": fac2, "legalName": "T-yangi"}))
    out = send(ctx, "super", sid, "tozala va nomini o‘zgartir")
    acts = out["messages"][1]["actions"]
    assert [a["tool"] for a in acts] == ["update_unit"]
    fake.say("Bekor qilindi.")
    assert batch(ctx, "super", [], [acts[0]["id"]]).status_code == 200
    refused = next(t for t in fake.calls[-1]["turns"] if t.role == "tool" and "alone_only" in t.content)
    assert refused.is_error
    # …and proposed alone, the batch route does not run it either — only the word typed on its own card does
    fake.call("reset_unit", {"unitId": fac2, "parts": ["orders"]})
    lone = send(ctx, "super", sid, "faqat tozala")["pending"]
    assert lone and lone["confirmWord"] and lone["danger"]
    assert batch(ctx, "super", [lone["id"]]).json()["error"]["code"] == "confirm_word_required"
    fake.say("Bekor qilindi.")
    assert batch(ctx, "super", [], [lone["id"]]).status_code == 200 and card(ctx, sid, "super")[0]["status"] == "cancelled"


def test_many_writes_are_taken_back_with_one_press(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    fac2, tag = ctx["facility2"], ctx["tag"]
    tid = new_thread(ctx, "admin", ctx["region"])
    before = unit(ctx, fac2)
    role = f"T-{tag} vaqtinchalik"
    many(fake, ("update_unit", {"unitId": fac2, "name": f"T-{tag} Yangi poliklinika"}), ("update_unit", {"unitId": fac2, "phone": "+998 62 555-55-55"}), ("create_role", {"name": role, "permissions": []}))
    acts = send(ctx, "admin", tid, "nom, telefon va rol")["messages"][1]["actions"]
    fake.say("Uchalasi bajarildi.")
    assert batch(ctx, "admin", [a["id"] for a in acts]).status_code == 200
    assert unit(ctx, fac2).name == f"T-{tag} Yangi poliklinika" and unit(ctx, fac2).phone == "+998 62 555-55-55" and role in roles(ctx)
    # a person changes the phone afterwards — that write is theirs now
    assert c.put(f"{API}/units/{fac2}", json={"phone": "+998 62 444-44-44"}, headers=h(ctx, "other")).status_code == 200

    asked = len(fake.calls)
    url = f"{API}/assistant/actions/batch-undo"
    assert c.post(url, json={"ids": [a["id"] for a in acts]}, headers=h(ctx, "other")).status_code == 404
    r = c.post(url, json={"ids": [a["id"] for a in acts]}, headers=h(ctx, "admin"))
    assert r.status_code == 200, r.text
    *origins, note = r.json()["messages"]
    # two went back, the one a person changed since stayed and is named; no model was asked
    assert len(fake.calls) == asked and note["undoOf"] and "2 ta amal orqaga qaytarildi" in note["content"] and "Qaytarilmadi (1 ta)" in note["content"]
    assert unit(ctx, fac2).name == before.name and unit(ctx, fac2).phone == "+998 62 444-44-44" and role not in roles(ctx)
    states = [bool(a["undo"]["undoneAt"]) for a in origins[0]["actions"]]
    assert len(origins) == 1 and states == [True, False, True]
    # nothing left that can go back → refused with the reason
    again = c.post(url, json={"ids": [acts[0]["id"], acts[2]["id"]]}, headers=h(ctx, "admin"))
    assert again.status_code == 409 and again.json()["error"]["code"] == "undo_done"


def test_an_unanswered_proposal_expires_whole_and_the_history_stays_whole(ctx: dict, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    tid = new_thread(ctx, "admin", ctx["region"])
    monkeypatch.setattr(service, "ACTION_TTL", timedelta(seconds=-1))
    ids = many(fake, ("update_unit", {"unitId": ctx["facility"], "legalName": "T-a"}), ("update_unit", {"unitId": ctx["facility2"], "legalName": "T-b"}))
    acts = send(ctx, "admin", tid, "ikkalasini o‘zgartir")["messages"][1]["actions"]
    assert batch(ctx, "admin", [a["id"] for a in acts]).json()["error"]["code"] == "action_expired"
    detail = c.get(f"{API}/assistant/threads/{tid}", headers=h(ctx, "admin")).json()
    assert detail["pending"] is None and [a["status"] for a in card(ctx, tid)] == ["expired", "expired"] and not [m for m in detail["messages"] if m["role"] == "tool"]
    assert batch(ctx, "admin", [a["id"] for a in acts]).json()["error"]["code"] == "action_resolved"
    # the next message finds every call answered
    fake.say("Davom etamiz.")
    send(ctx, "admin", tid, "mayli")
    made, got = answered(fake)
    assert sorted(made) == sorted(got) == sorted(ids)
