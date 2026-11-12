"""The AI assistant («Yordamchi»): the model only gets the tools the employee holds, read tools run at once, a
write waits for the employee's confirmation and then runs with THEIR rights, secrets never reach the model, and
the account (provider / model / key) is inherited down the tree. The model itself is a scripted fake."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.crypto import encrypt
from app.infrastructure.db.models import AssistantAccount, AssistantAction, AssistantMessage, OrgUnit
from app.infrastructure.redis.client import get_redis
from app.modules.assistant import service
from app.modules.assistant.providers import Reply, ToolCall
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.helpers import API, app_instance, bearer, db, make_employee, make_tree, seed, staff_token


class FakeProvider:
    """Answers from a script; remembers what it was asked (system prompt, turns, tool names offered)."""

    def __init__(self) -> None:
        self.script: list[Reply] = []
        self.calls: list[dict[str, Any]] = []

    def say(self, text: str) -> None:
        self.script.append(Reply(text=text, tool_calls=[], stop="end", usage={"input": 1, "output": 1}))

    def call(self, name: str, args: dict[str, Any], *, text: str = "") -> None:
        self.script.append(
            Reply(
                text=text,
                tool_calls=[ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=name, args=args)],
                stop="tool",
                usage={"input": 1, "output": 1},
            )
        )

    async def complete(self, *, system: str, turns: list[Any], tools: list[dict[str, Any]]) -> Reply:
        self.calls.append({"system": system, "turns": turns, "tools": [t["name"] for t in tools]})
        if not self.script:
            return Reply(text="(skript tugadi)", tool_calls=[], stop="end", usage={})
        return self.script.pop(0)


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        s.add(
            AssistantAccount(
                unit_id=a.region.id,
                provider="openai",
                api_key_enc=encrypt("sk-test-a"),
                api_key_masked="sk-t••••st-a",
                settings={"effort": "low"},
            )
        )
        sup = await make_employee(s, None, super_admin=True)
        admin_a = await make_employee(s, a.region, "admin")
        admin_b = await make_employee(s, b.region, "admin")
        reg = await make_employee(s, a.facility, "registrator")
        vrach = await make_employee(s, a.facility, "vrach")
        await s.flush()
        return {
            "a": {k: str(getattr(a, k).id) for k in ("region", "district", "facility")},
            "b": {k: str(getattr(b, k).id) for k in ("region", "district", "facility")},
            "tag": a.tag,
            "vrach_id": str(vrach.id),
            "logins": {"super": sup.login, "admin-a": admin_a.login, "admin-b": admin_b.login, "reg": reg.login, "vrach": vrach.login},
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
    return f


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


def new_thread(ctx: dict, who: str, unit_id: str) -> str:
    r = ctx["client"].post(f"{API}/assistant/threads", params={"unitId": unit_id}, json={}, headers=h(ctx, who))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def send(ctx: dict, who: str, thread_id: str, text: str, unit_id: str | None = None) -> dict:
    body = {"content": text, **({"unitId": unit_id} if unit_id else {})}
    r = ctx["client"].post(f"{API}/assistant/threads/{thread_id}/messages", json=body, headers=h(ctx, who))
    assert r.status_code == 200, r.text
    return r.json()


def reset_rate_limit(c: TestClient) -> None:
    """Forget the assistant's per-employee request counters (fake redis lives for the whole process)."""

    async def _run() -> None:
        r = get_redis()
        keys = await r.keys("assistant:rl:*")
        if keys:
            await r.delete(*keys)

    c.portal.call(_run)


# ----------------------------------------------------------------------------- status & tools


def test_status_offers_only_the_tools_the_role_holds(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    admin = c.get(f"{API}/assistant/status", params={"unitId": ctx["a"]["region"]}, headers=h(ctx, "admin-a")).json()
    assert admin["enabled"] and admin["configured"] and admin["provider"] == "openai" and admin["model"] == "gpt-4.1"
    names = {t["name"] for t in admin["tools"]}
    assert {
        "whoami",
        "list_units",
        "list_employees",
        "update_unit",
        "create_employee",
        "dashboard",
        "send_sms",
    } <= names
    reg = c.get(f"{API}/assistant/status", params={"unitId": ctx["a"]["facility"]}, headers=h(ctx, "reg")).json()
    reg_names = {t["name"] for t in reg["tools"]}
    assert {"whoami", "list_orders", "search_patients", "list_service_types", "send_sms"} <= reg_names
    assert not ({"list_employees", "update_unit", "create_employee", "update_service_type"} & reg_names)
    # the other region has no account above it
    other = c.get(f"{API}/assistant/status", params={"unitId": ctx["b"]["region"]}, headers=h(ctx, "admin-b")).json()
    assert other["enabled"] and not other["configured"]


def test_account_is_inherited_and_the_key_never_comes_back(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    fac = c.get(f"{API}/units/{ctx['a']['facility']}/assistant", headers=h(ctx, "admin-a")).json()
    assert (
        fac["mode"] == "inherited"
        and fac["provider"] == "openai"
        and fac["effectiveUnitId"] == ctx["a"]["region"]
        and fac["configured"]
    )
    assert "sk-test-a" not in json.dumps(fac) and fac["apiKeyMasked"].startswith("sk-t")
    # the facility gets its own DeepSeek account, with the preset model
    r = c.put(
        f"{API}/units/{ctx['a']['facility']}/assistant",
        json={"mode": "own", "provider": "deepseek", "apiKey": "ds-key-123456"},
        headers=h(ctx, "admin-a"),
    )
    assert r.status_code == 200, r.text
    own = r.json()
    assert (
        own["mode"] == "own"
        and own["provider"] == "deepseek"
        and own["model"] == "deepseek-chat"
        and own["baseUrl"] == "https://api.deepseek.com"
    )
    assert "ds-key" not in json.dumps(own).replace(own["apiKeyMasked"], "")
    # a custom gateway needs its base URL; a key is required the first time
    r = c.put(
        f"{API}/units/{ctx['a']['district']}/assistant",
        json={"mode": "own", "provider": "custom", "apiKey": "x" * 12},
        headers=h(ctx, "admin-a"),
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "ai_base_url_required"
    r = c.put(
        f"{API}/units/{ctx['a']['district']}/assistant",
        json={"mode": "own", "provider": "anthropic"},
        headers=h(ctx, "admin-a"),
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "ai_api_key_required"
    # back to inheriting
    assert (
        c.put(
            f"{API}/units/{ctx['a']['facility']}/assistant", json={"mode": "inherited"}, headers=h(ctx, "admin-a")
        ).json()["mode"]
        == "inherited"
    )
    # a registrar may not read or change it
    assert c.get(f"{API}/units/{ctx['a']['facility']}/assistant", headers=h(ctx, "reg")).status_code == 403
    assert (
        c.put(f"{API}/units/{ctx['a']['facility']}/assistant", json={"mode": "off"}, headers=h(ctx, "reg")).status_code
        == 403
    )


def test_the_platform_account_serves_everyone_without_a_closer_one(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    b_fac = ctx["b"]["facility"]
    # tree B has no account anywhere above: nothing works there
    st = c.get(f"{API}/assistant/status", params={"unitId": b_fac}, headers=h(ctx, "admin-b")).json()
    assert st["enabled"] and not st["configured"]
    # only a superadmin may touch the platform account
    assert c.get(f"{API}/assistant/platform-account", headers=h(ctx, "admin-a")).status_code == 403
    r = c.put(f"{API}/assistant/platform-account", json={"mode": "off"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "platform_only"
    assert c.get(f"{API}/assistant/platform-account", headers=h(ctx, "super")).json()["mode"] == "inherited"
    r = c.put(
        f"{API}/assistant/platform-account",
        json={"mode": "own", "provider": "deploy", "apiKey": "dp-key-123456", "maxTokens": 0},
        headers=h(ctx, "super"),
    )
    assert r.status_code == 200, r.text
    pl = r.json()
    assert (
        pl["mode"] == "own"
        and pl["provider"] == "deploy"
        and pl["model"] == "deepseek-ai/deepseek-v4.1-flash-1017"
        and pl["effectivePlatform"]
        and pl["effectiveUnitId"] is None
        and pl["configured"]
    )
    assert "dp-key" not in json.dumps(pl).replace(pl["apiKeyMasked"], "")
    try:
        # tree B now works through the platform account — nobody there entered anything
        st = c.get(f"{API}/assistant/status", params={"unitId": b_fac}, headers=h(ctx, "admin-b")).json()
        assert st["configured"] and st["provider"] == "deploy" and st["effectivePlatform"]
        acc = c.get(f"{API}/units/{ctx['b']['region']}/assistant", headers=h(ctx, "admin-b")).json()
        assert acc["mode"] == "inherited" and acc["effectivePlatform"] and acc["configured"] and acc["provider"] == "deploy"
        # tree A keeps its own account — the closer one wins
        acc = c.get(f"{API}/units/{ctx['a']['facility']}/assistant", headers=h(ctx, "admin-a")).json()
        assert acc["provider"] == "openai" and acc["effectiveUnitId"] == ctx["a"]["region"] and not acc["effectivePlatform"]
        # a unit that switches the assistant off is not rescued by the platform account
        assert c.put(f"{API}/units/{ctx['b']['region']}/assistant", json={"mode": "off"}, headers=h(ctx, "admin-b")).status_code == 200
        st = c.get(f"{API}/assistant/status", params={"unitId": b_fac}, headers=h(ctx, "admin-b")).json()
        assert not st["configured"]
        assert c.put(f"{API}/units/{ctx['b']['region']}/assistant", json={"mode": "inherited"}, headers=h(ctx, "admin-b")).status_code == 200
        assert c.get(f"{API}/assistant/status", params={"unitId": b_fac}, headers=h(ctx, "admin-b")).json()["configured"]
    finally:
        # back to no platform account
        assert c.put(f"{API}/assistant/platform-account", json={"mode": "inherited"}, headers=h(ctx, "super")).json()["mode"] == "inherited"
    assert not c.get(f"{API}/assistant/status", params={"unitId": b_fac}, headers=h(ctx, "admin-b")).json()["configured"]


def test_export_tools_build_a_file_the_owner_downloads(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    tid = new_thread(ctx, "admin-a", ctx["a"]["region"])
    fake.call(
        "export_data",
        {
            "dataset": "units",
            "format": "csv",
            "title": "Muassasalar",
            "params": {"search": ctx["tag"]},
            "filters": [{"column": "level", "op": "eq", "value": "facility"}],
            "sort": [{"column": "name", "dir": "desc"}],
            "columns": [{"key": "name", "title": "Nomi"}, "level", "code"],
        },
    )
    fake.say("Fayl tayyor.")
    out = send(ctx, "admin-a", tid, "Muassasalarni CSV qilib ber")
    msg = out["messages"][1]
    call = msg["toolCalls"][0]
    assert call["name"] == "export_data" and call["status"] == "ok" and call["file"]["name"].endswith(".csv")
    assert msg["files"][0]["id"] == call["file"]["id"] and msg["files"][0]["rows"] >= 1
    # the model got the file's card and a preview — never the bytes
    result = fake.calls[1]["turns"][-1].content
    assert '"file"' in result and "preview" in result and "PK" not in result
    fid = call["file"]["id"]
    r = c.get(f"{API}/assistant/files/{fid}", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("\ufeff") and text.lstrip("\ufeff").startswith("Nomi;Level;Code") and ctx["tag"].upper() in text.upper()
    # only its owner; the thread view carries it too
    assert c.get(f"{API}/assistant/files/{fid}", headers=h(ctx, "admin-b")).status_code == 404
    detail = c.get(f"{API}/assistant/threads/{tid}", headers=h(ctx, "admin-a")).json()
    assert any(m["files"] and m["files"][0]["id"] == fid for m in detail["messages"])
    # a table the model composed → Excel, sorted on the server
    fake.call(
        "export_table",
        {
            "title": "Xulosa",
            "columns": [{"key": "nomi", "title": "Nomi"}, {"key": "soni", "title": "Soni"}],
            "rows": [{"nomi": "A", "soni": 2}, ["B", 5]],
            "sort": [{"column": "soni", "dir": "desc"}],
        },
    )
    fake.say("Excel tayyor.")
    out = send(ctx, "admin-a", tid, "Shuni Excel qil")
    f2 = out["messages"][1]["toolCalls"][0]["file"]
    assert f2["name"].endswith(".xlsx") and f2["rows"] == 2
    r = c.get(f"{API}/assistant/files/{f2['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert '"preview": [{"nomi": "B", "soni": 5}' in fake.calls[3]["turns"][-1].content
    # an unknown column is an error result the model can act on (it lists the columns); no file
    fake.call("export_data", {"dataset": "units", "columns": ["nope"]})
    fake.say("Ustun topilmadi.")
    out = send(ctx, "admin-a", tid, "nope ustunini chiqar")
    bad = out["messages"][1]["toolCalls"][0]
    assert bad["status"] == "error" and not bad.get("file")
    assert "availableColumns" in fake.calls[5]["turns"][-1].content
    # a dataset the employee may not read is refused; a role without reports.export is not offered the tools at all
    fake.call("export_data", {"dataset": "employees"})
    fake.say("Ruxsat yo‘q.")
    out = send(ctx, "reg", new_thread(ctx, "reg", ctx["a"]["facility"]), "Xodimlarni Excel qil", unit_id=ctx["a"]["facility"])
    assert out["messages"][1]["toolCalls"][0]["status"] == "error"
    st = c.get(f"{API}/assistant/status", params={"unitId": ctx["a"]["facility"]}, headers=h(ctx, "vrach")).json()
    assert "export_data" not in {t["name"] for t in st["tools"]} and "export_table" not in {t["name"] for t in st["tools"]}


def test_documents_and_charts_are_files_too(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    tid = new_thread(ctx, "admin-a", ctx["a"]["region"])
    # a report the model wrote → Word
    fake.call("export_document", {"title": "Oylik hisobot", "content": "# Oylik hisobot\n\nKirish **qisqa**.\n\n## Ko‘rsatkichlar\n- cheklar: 120\n- tushum: 1 200 000\n\n| Nomi | Soni |\n|---|---|\n| A | 1 |\n", "format": "word"})
    fake.say("Hisobot tayyor.")
    out = send(ctx, "admin-a", tid, "Oylik hisobotni Word qilib ber")
    f = out["messages"][1]["toolCalls"][0]["file"]
    assert f["name"] == "oylik-hisobot-" + f["createdAt"][:10] + ".docx" or f["name"].endswith(".docx")
    r = c.get(f"{API}/assistant/files/{f['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.content[:2] == b"PK" and "wordprocessingml" in r.headers["content-type"]
    assert '"sections": ["Ko‘rsatkichlar"]' in fake.calls[1]["turns"][-1].content
    # the same as PDF
    fake.call("export_document", {"title": "Oylik hisobot", "content": "Matn.", "format": "pdf"})
    fake.say("PDF tayyor.")
    out = send(ctx, "admin-a", tid, "PDF ham")
    f = out["messages"][1]["toolCalls"][0]["file"]
    r = c.get(f"{API}/assistant/files/{f['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    # a chart from data the model gives → PNG, previewable in the chat
    fake.call("export_chart", {"title": "Tushum", "type": "bar", "labels": ["A", "B"], "series": [{"name": "so‘m", "values": [1200000, 540000]}]})
    fake.say("Diagramma tayyor.")
    out = send(ctx, "admin-a", tid, "Diagramma qil")
    f = out["messages"][1]["toolCalls"][0]["file"]
    assert f["name"].endswith(".png") and f["rows"] == 2
    r = c.get(f"{API}/assistant/files/{f['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG" and r.headers["content-type"] == "image/png"
    # a chart straight from a dataset (units of the fixture tree, counted by level) → SVG
    fake.call("export_chart", {"title": "Birliklar", "type": "bar", "format": "svg", "dataset": "units", "params": {"search": ctx["tag"]}, "x": "name", "y": ["counts.facilities"], "limit": 5})
    fake.say("SVG tayyor.")
    out = send(ctx, "admin-a", tid, "Birliklar diagrammasi")
    call = out["messages"][1]["toolCalls"][0]
    if call["status"] == "ok":
        r = c.get(f"{API}/assistant/files/{call['file']['id']}", headers=h(ctx, "admin-a"))
        assert r.status_code == 200 and r.content.startswith(b"<svg") and r.headers["content-type"].startswith("image/svg")
    else:
        # the fixture's unit DTO has no such column: the model gets the available ones back
        assert "availableColumns" in fake.calls[7]["turns"][-1].content
    # bad data is an error the model can fix, not a 500
    fake.call("export_chart", {"title": "x", "type": "pie", "labels": [], "series": []})
    fake.say("Ma’lumot yo‘q.")
    out = send(ctx, "admin-a", tid, "bo‘sh diagramma")
    assert out["messages"][1]["toolCalls"][0]["status"] == "error"


# ----------------------------------------------------------------------------- reading


def test_a_read_tool_runs_at_once_and_the_model_sees_only_allowed_tools(ctx: dict, fake: FakeProvider) -> None:
    tid = new_thread(ctx, "admin-a", ctx["a"]["region"])
    fake.call("list_units", {"search": ctx["tag"], "level": "facility"}, text="Qidiryapman…")
    fake.say("Topildi: bitta muassasa.")
    out = send(ctx, "admin-a", tid, "Mening viloyatimdagi muassasalarni ko‘rsat")
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant", "assistant"]
    call = out["messages"][1]["toolCalls"][0]
    assert call["name"] == "list_units" and call["kind"] == "read" and call["status"] == "ok"
    assert out["messages"][2]["content"] == "Topildi: bitta muassasa." and out["pending"] is None
    # what the model was given: the system prompt names the employee and the unit, the tools are the admin's set
    first = fake.calls[0]
    assert ctx["tag"] in first["system"] and "list_employees" in first["tools"] and "whoami" in first["tools"]
    # the second round carried the tool result with the facility in it
    turns = fake.calls[1]["turns"]
    assert turns[-1].role == "tool" and f"T-{ctx['tag']} SES" in turns[-1].content
    # the thread keeps it all; the title comes from the first message
    detail = ctx["client"].get(f"{API}/assistant/threads/{tid}", headers=h(ctx, "admin-a")).json()
    assert detail["thread"]["title"].startswith("Mening viloyatimdagi") and len(detail["messages"]) == 3


def test_whoami_and_secrets_are_stripped(ctx: dict, fake: FakeProvider) -> None:
    tid = new_thread(ctx, "admin-a", ctx["a"]["region"])
    fake.call("whoami", {})
    fake.call("get_employee", {"employeeId": ctx["vrach_id"]})
    fake.say("ok")
    send(ctx, "admin-a", tid, "men kimman, vrach kim")
    who = json.loads(fake.calls[1]["turns"][-1].content)
    assert who["role"]["key"] == "admin" and "update_unit" in who["tools"] and "assistant.use" in who["permissions"]
    emp = fake.calls[2]["turns"][-1].content
    assert "passwordHash" not in emp and "password" not in emp.lower() and ctx["vrach_id"] in emp


# ----------------------------------------------------------------------------- writing


def _phone(client: TestClient, unit_id: str) -> str | None:
    async def q(s):
        return (await s.get(OrgUnit, uuid.UUID(unit_id))).phone

    return db(client, q)


def test_a_write_waits_for_the_yes_and_then_runs_with_the_employees_rights(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    fac = ctx["a"]["facility"]
    tid = new_thread(ctx, "admin-a", ctx["a"]["region"])
    fake.call("update_unit", {"unitId": fac, "phone": "+998 62 777-77-77"}, text="Telefonni yangilayman, tasdiqlang.")
    out = send(ctx, "admin-a", tid, "Muassasa telefonini +998 62 777-77-77 ga o‘zgartir")
    pending = out["pending"]
    assert (
        pending
        and pending["tool"] == "update_unit"
        and pending["status"] == "pending"
        and "phone=+998 62 777-77-77" in pending["summary"]
    )
    assert (
        out["messages"][1]["toolCalls"][0]["status"] == "pending"
        and out["messages"][1]["action"]["id"] == pending["id"]
    )
    assert _phone(c, fac) != "+998 62 777-77-77"
    # another employee cannot confirm it
    assert c.post(f"{API}/assistant/actions/{pending['id']}/confirm", headers=h(ctx, "admin-b")).status_code == 404
    fake.say("Telefon yangilandi.")
    r = c.post(f"{API}/assistant/actions/{pending['id']}/confirm", headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    done = r.json()
    assert _phone(c, fac) == "+998 62 777-77-77"
    assert done["pending"] is None and done["messages"][-1]["content"] == "Telefon yangilandi."
    # the model learned the result, the action is done, a second confirm is refused
    assert fake.calls[-1]["turns"][-1].role == "tool" and "+998 62 777-77-77" in fake.calls[-1]["turns"][-1].content
    detail = c.get(f"{API}/assistant/threads/{tid}", headers=h(ctx, "admin-a")).json()
    assert detail["messages"][1]["action"]["status"] == "done" and detail["pending"] is None
    assert c.post(f"{API}/assistant/actions/{pending['id']}/confirm", headers=h(ctx, "admin-a")).status_code == 409


def test_cancel_and_moving_on_both_drop_the_pending_write(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    tid = new_thread(ctx, "admin-a", ctx["a"]["region"])
    fake.call("update_unit", {"unitId": ctx["a"]["facility"], "name": "T-x boshqa nom"})
    first = send(ctx, "admin-a", tid, "nomini o‘zgartir")
    fake.say("Bekor qilindi.")
    r = c.post(f"{API}/assistant/actions/{first['pending']['id']}/cancel", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["messages"][-1]["content"] == "Bekor qilindi."
    assert fake.calls[-1]["turns"][-1].is_error and "bekor" in fake.calls[-1]["turns"][-1].content.lower()
    # a new pending write, then the employee simply writes something else — the write is cancelled, never run
    fake.call("update_unit", {"unitId": ctx["a"]["facility"], "name": "T-y yana"})
    second = send(ctx, "admin-a", tid, "yana nom")
    fake.say("Yaxshi.")
    send(ctx, "admin-a", tid, "yo‘q, qoldir")

    async def st(s):
        return (await s.get(AssistantAction, uuid.UUID(second["pending"]["id"]))).status

    assert db(c, st) == "cancelled"
    assert (
        c.post(f"{API}/assistant/actions/{second['pending']['id']}/confirm", headers=h(ctx, "admin-a")).status_code
        == 409
    )


def test_the_territory_and_the_role_are_enforced_on_every_tool(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    # a region administrator asks about the other region: the tool answers 403, nothing leaks
    tid = new_thread(ctx, "admin-a", ctx["a"]["region"])
    fake.call("get_unit", {"unitId": ctx["b"]["facility"]})
    fake.say("Bu birlik sizning hududingizdan tashqarida.")
    send(ctx, "admin-a", tid, "B muassasasini ko‘rsat")
    res = fake.calls[1]["turns"][-1]
    assert res.is_error and json.loads(res.content)["error"] == "forbidden"
    # a registrar: the model is not offered update_unit; even if it calls it, nothing is proposed or changed
    tid2 = new_thread(ctx, "reg", ctx["a"]["facility"])
    fake.call("update_unit", {"unitId": ctx["a"]["facility"], "name": "T-hack"})
    fake.say("Sizda bu huquq yo‘q.")
    out = send(ctx, "reg", tid2, "muassasa nomini o‘zgartir")
    assert out["pending"] is None and "update_unit" not in fake.calls[-2]["tools"]
    assert fake.calls[-1]["turns"][-1].is_error

    async def q(s):
        name = (await s.get(OrgUnit, uuid.UUID(ctx["a"]["facility"]))).name
        actions = (
            (await s.execute(select(AssistantAction).where(AssistantAction.thread_id == uuid.UUID(tid2))))
            .scalars()
            .all()
        )
        return name, len(actions)

    name, n = db(c, q)
    assert name != "T-hack" and n == 0
    # the thread of one employee is invisible to another; a region without an account gets a clear 409
    assert c.get(f"{API}/assistant/threads/{tid}", headers=h(ctx, "admin-b")).status_code == 404
    tid3 = new_thread(ctx, "admin-b", ctx["b"]["region"])
    r = c.post(f"{API}/assistant/threads/{tid3}/messages", json={"content": "salom"}, headers=h(ctx, "admin-b"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "assistant_not_configured"


def test_rate_limit_and_tool_round_cap(ctx: dict, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    monkeypatch.setattr(service, "RATE_LIMIT", 2)
    monkeypatch.setattr(service, "RATE_WINDOW_SECONDS", 600)  # long: the tool rounds below are slow over a tunnel
    reset_rate_limit(c)
    tid = new_thread(ctx, "super", ctx["a"]["region"])
    for _ in range(8):
        fake.call("whoami", {})
    fake.say("never reached")
    out = send(ctx, "super", tid, "loop", unit_id=ctx["a"]["region"])
    assert "chegara" in out["messages"][-1]["content"].lower()
    assert len([m for m in out["messages"] if m["role"] == "assistant" and m["toolCalls"]]) == service.MAX_TOOL_ROUNDS
    fake.say("ok")
    send(ctx, "super", tid, "two", unit_id=ctx["a"]["region"])
    r = c.post(
        f"{API}/assistant/threads/{tid}/messages",
        json={"content": "three", "unitId": ctx["a"]["region"]},
        headers=h(ctx, "super"),
    )
    assert r.status_code == 429
    reset_rate_limit(c)

    async def count(s):
        return len(
            (await s.execute(select(AssistantMessage).where(AssistantMessage.thread_id == uuid.UUID(tid))))
            .scalars()
            .all()
        )

    assert db(c, count) > 10
