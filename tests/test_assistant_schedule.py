"""Scheduled tasks of the assistant («Rejali topshiriqlar»): the rule that says when a task is due, the owner's own
list, and the run — as the owner with the rights of that moment, read-only tools, the answer in the task's own
conversation with a notice. A task never outlives its owner's rights, a failing one switches itself off, and a
crash can never run one occurrence twice. The model is a scripted fake; the worker's tick is called by hand."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from app.api.deps import invalidate_principal_cache
from app.core.crypto import encrypt
from app.core.exceptions import ExternalServiceError
from app.infrastructure.db.models import AssistantAccount, AssistantSchedule, Employee, OrgUnit
from app.modules.assistant import schedule as sch
from app.modules.assistant import schedule_runner as runner
from app.modules.assistant import service
from app.modules.assistant import tools as tl
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.helpers import API, app_instance, db, make_employee, make_tree, seed, staff_token
from tests.test_assistant import FakeProvider, h, new_thread, send

TASHKENT = ZoneInfo("Asia/Tashkent")
DAILY = {"title": "T-kechagi hisobot", "prompt": "Kechagi kun bo‘yicha tushum hisobotini tayyorla", "kind": "daily", "time": "08:00"}


def at(y: int, mo: int, d: int, hh: int, mm: int = 0) -> datetime:
    """A wall-clock moment in Tashkent, as UTC."""
    return datetime(y, mo, d, hh, mm, tzinfo=TASHKENT).astimezone(UTC)


# ----------------------------------------------------------------------------- the rule (no database)


def test_the_rule_points_at_the_next_wall_clock_moment() -> None:
    nxt = lambda kind, t, after, **kw: sch.next_run(kind, t, kw.get("weekdays"), kw.get("month_day"), kw.get("run_date"), TASHKENT, after)  # noqa: E731
    # 2026-11-23 is a Monday
    assert nxt("daily", "08:00", at(2026, 11, 23, 7)) == at(2026, 11, 23, 8)
    assert nxt("daily", "08:00", at(2026, 11, 23, 8)) == at(2026, 11, 24, 8)  # strictly after: the same minute is not due again
    assert nxt("weekly", "09:30", at(2026, 11, 23, 10), weekdays=[1, 5]) == at(2026, 11, 27, 9, 30)
    assert nxt("weekly", "09:30", at(2026, 11, 27, 9, 30), weekdays=[1, 5]) == at(2026, 11, 30, 9, 30)
    # a month shorter than the day asked for runs on its last day
    assert nxt("monthly", "10:00", at(2026, 2, 10, 12), month_day=31) == at(2026, 2, 28, 10)
    assert nxt("monthly", "10:00", at(2026, 2, 28, 10), month_day=31) == at(2026, 3, 31, 10)
    assert nxt("monthly", "10:00", at(2026, 12, 31, 11), month_day=1) == at(2027, 1, 1, 10)
    assert nxt("once", "09:00", at(2026, 11, 23, 7), run_date=date(2026, 11, 24)) == at(2026, 11, 24, 9)
    assert nxt("once", "09:00", at(2026, 11, 24, 9), run_date=date(2026, 11, 24)) is None
    # another zone keeps its own clock (Berlin is UTC+1 in November)
    assert sch.next_run("daily", "08:00", None, None, None, ZoneInfo("Europe/Berlin"), datetime(2026, 11, 23, 6, tzinfo=UTC)) == datetime(2026, 11, 23, 7, tzinfo=UTC)
    assert sch.zone("No/Such_Zone") is sch.DEFAULT_TZ and sch.zone(None) is sch.DEFAULT_TZ


def test_the_rule_reads_like_a_sentence() -> None:
    assert sch.when_text("daily", "08:00", [], None, None) == "Har kuni 08:00"
    assert sch.when_text("weekly", "09:30", [1, 2, 3, 4, 5], None, None) == "Ish kunlari (dushanba–juma) 09:30"
    assert sch.when_text("weekly", "09:30", [5, 1], None, None) == "Har dushanba, juma 09:30"
    assert sch.when_text("monthly", "10:00", [], 1, None) == "Har oyning 1-kuni 10:00"
    assert sch.when_text("once", "09:00", [], None, date(2026, 11, 24)) == "Bir marta: 24.11.2026 09:00"
    # the task tools are every assistant user's own; a scheduled run is offered none of the writes
    assert {"list_schedules", "create_schedule", "update_schedule", "delete_schedule"} <= set(tl.REGISTRY)
    assert all(tl.REGISTRY[n].perms == ("assistant.use",) and tl.REGISTRY[n].group == "schedule" for n in ("list_schedules", "create_schedule", "update_schedule", "delete_schedule"))


# ----------------------------------------------------------------------------- the list and the run


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        s.add(AssistantAccount(unit_id=a.region.id, provider="openai", api_key_enc=encrypt("sk-test"), api_key_masked="sk-t••••test", settings={}))
        admin = await make_employee(s, a.region, "admin")
        other = await make_employee(s, a.region, "admin")
        reg = await make_employee(s, a.facility, "registrator")
        await s.flush()
        return {
            "tag": a.tag, "region": str(a.region.id), "facility": str(a.facility.id), "elsewhere": str(b.facility.id),
            "ids": {"admin": str(admin.id), "other": str(other.id), "reg": str(reg.id)}, "logins": {"admin": admin.login, "other": other.login, "reg": reg.login},
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


def create(ctx: dict, who: str, unit_id: str | None = None, **over: Any):
    return ctx["client"].post(f"{API}/assistant/schedules", params={"unitId": unit_id or ctx["facility"]}, json={**DAILY, **over}, headers=h(ctx, who))


def listed(ctx: dict, who: str) -> list[dict[str, Any]]:
    r = ctx["client"].get(f"{API}/assistant/schedules", headers=h(ctx, who))
    assert r.status_code == 200, r.text
    return r.json()["items"]


def one(ctx: dict, who: str, sid: str) -> dict[str, Any]:
    return next(x for x in listed(ctx, who) if x["id"] == sid)


def make_due(ctx: dict, sid: str) -> None:
    async def run(s):
        row = await s.get(AssistantSchedule, uuid.UUID(sid))
        row.next_run_at = datetime.now(UTC) - timedelta(minutes=1)

    db(ctx["client"], run)


def tick(ctx: dict, sid: str) -> str | None:
    """What the worker does with one task (its own tick takes every due task of the database — other runs' too)."""
    return ctx["client"].portal.call(runner.run_one, uuid.UUID(sid))


def notices(ctx: dict, who: str) -> list[dict[str, Any]]:
    return ctx["client"].get(f"{API}/notifications", headers=h(ctx, who)).json()


def wipe(ctx: dict, who: str) -> None:
    for x in listed(ctx, who):
        assert ctx["client"].delete(f"{API}/assistant/schedules/{x['id']}", headers=h(ctx, who)).status_code == 204


def test_the_owner_keeps_their_own_list_of_tasks(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    r = create(ctx, "admin")
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["when"] == "Har kuni 08:00" and task["isActive"] and task["timezone"] == "Asia/Tashkent" and task["unitId"] == ctx["facility"] and task["unitName"] and task["threadId"] is None
    nxt = datetime.fromisoformat(task["nextRunAt"].replace("Z", "+00:00"))
    assert timedelta(0) < nxt - datetime.now(UTC) <= timedelta(days=1) and nxt.astimezone(TASHKENT).strftime("%H:%M") == "08:00"
    assert [x["id"] for x in listed(ctx, "admin")] == [task["id"]] and c.get(f"{API}/assistant/schedules", headers=h(ctx, "admin")).json()["limit"] == sch.MAX_SCHEDULES
    # a task is its owner's: for a colleague of the same unit it does not exist
    url = f"{API}/assistant/schedules/{task['id']}"
    assert listed(ctx, "other") == []
    assert c.patch(url, json={"title": "T-meniki"}, headers=h(ctx, "other")).status_code == 404
    assert c.delete(url, headers=h(ctx, "other")).status_code == 404 and c.post(f"{url}/run", headers=h(ctx, "other")).status_code == 404
    # the rule is checked; the unit must lie in the owner's territory
    assert create(ctx, "admin", kind="weekly").json()["error"]["code"] == "schedule_weekdays"
    assert create(ctx, "admin", kind="monthly").json()["error"]["code"] == "schedule_month_day"
    assert create(ctx, "admin", kind="once", runDate="2020-01-01").json()["error"]["code"] == "schedule_past"
    assert create(ctx, "admin", time="25:00").status_code == 422
    assert create(ctx, "admin", ctx["elsewhere"]).status_code == 403
    # paused: nothing ahead; the rule may change meanwhile; switched back on: the next moment of the new rule
    off = c.patch(url, json={"isActive": False}, headers=h(ctx, "admin")).json()
    assert off["isActive"] is False and off["nextRunAt"] is None
    changed = c.patch(url, json={"kind": "weekly", "weekdays": [5, 1], "time": "09:30", "title": "T-haftalik"}, headers=h(ctx, "admin")).json()
    assert changed["when"] == "Har dushanba, juma 09:30" and changed["weekdays"] == [1, 5] and changed["nextRunAt"] is None and changed["title"] == "T-haftalik"
    on = c.patch(url, json={"isActive": True}, headers=h(ctx, "admin")).json()
    assert on["isActive"] and datetime.fromisoformat(on["nextRunAt"].replace("Z", "+00:00")).astimezone(TASHKENT).isoweekday() in (1, 5)
    # every run is a paid model call — the list has a ceiling
    monkeypatch.setattr(sch, "MAX_SCHEDULES", 1)
    assert create(ctx, "admin").json()["error"]["code"] == "schedule_limit"
    assert c.delete(url, headers=h(ctx, "admin")).status_code == 204 and listed(ctx, "admin") == []
    assert c.patch(url, json={"title": "T-yo‘q"}, headers=h(ctx, "admin")).status_code == 404


def test_a_due_task_runs_as_its_owner_reads_only_and_tells_them(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    wipe(ctx, "admin")
    sid = create(ctx, "admin").json()["id"]
    assert tick(ctx, sid) is None  # not due yet — the worker leaves it alone
    make_due(ctx, sid)
    # the model tries a write: a scheduled run is not even offered one, so nothing is proposed and nothing changes
    fake.call("update_unit", {"unitId": ctx["facility"], "name": "T-buzilgan nom"})
    fake.call("whoami", {})
    fake.say("Kechagi tushum: 0 so‘m, 0 ta chek.")
    assert tick(ctx, sid) == "ok"
    asked = fake.calls[-1]
    assert asked["tools"] and all(tl.REGISTRY[n].kind == "read" for n in asked["tools"]) and tl.ASK_TOOL not in asked["tools"] and "whoami" in asked["tools"]
    assert "REJALI ISHGA TUSHIRISH" in asked["system"] and "Asia/Tashkent" in asked["system"]
    assert db(c, lambda s: s.get(OrgUnit, uuid.UUID(ctx["facility"]))).name != "T-buzilgan nom"

    task = one(ctx, "admin", sid)
    assert task["lastStatus"] == "ok" and task["runCount"] == 1 and task["threadId"] and task["lastError"] is None and task["isActive"]
    assert datetime.fromisoformat(task["nextRunAt"].replace("Z", "+00:00")) > datetime.now(UTC)
    assert tick(ctx, sid) is None  # the occurrence is spent — a second worker finds nothing to run

    # the answer waits in the task's own conversation, unread, and a notice leads there
    thread = next(t for t in c.get(f"{API}/assistant/threads", headers=h(ctx, "admin")).json() if t["id"] == task["threadId"])
    assert thread["scheduleId"] == sid and thread["unread"] is True and thread["title"] == f"⏰ {DAILY['title']}"
    note = next(n for n in notices(ctx, "admin") if n["link"] == f"/assistant?c={task['threadId']}")
    assert note["title"] == f"⏰ {DAILY['title']}" and "0 so‘m" in note["body"] and note["kind"] == "success" and not note["read"]
    assert not [n for n in notices(ctx, "other") if n["link"] == note["link"]]
    detail = c.get(f"{API}/assistant/threads/{task['threadId']}", headers=h(ctx, "admin")).json()
    first, last = detail["messages"][0], detail["messages"][-1]
    assert first["role"] == "user" and first["scheduled"] is True and first["content"] == DAILY["prompt"] and last["content"] == "Kechagi tushum: 0 so‘m, 0 ta chek."
    assert detail["pending"] is None and not [m for m in detail["messages"] if m["actions"]]
    # opening it reads it; the owner may go on talking there, with all their tools back
    assert next(t for t in c.get(f"{API}/assistant/threads", headers=h(ctx, "admin")).json() if t["id"] == task["threadId"])["unread"] is False
    fake.say("Albatta.")
    send(ctx, "admin", task["threadId"], "Batafsilroq yoz")
    assert "update_unit" in fake.calls[-1]["tools"] and "REJALI ISHGA TUSHIRISH" not in fake.calls[-1]["system"]
    # the next run lands in the same conversation; one the owner deleted is not brought back — a new one starts
    make_due(ctx, sid)
    fake.say("Bugungi hisobot.")
    assert tick(ctx, sid) == "ok" and one(ctx, "admin", sid)["threadId"] == task["threadId"] and one(ctx, "admin", sid)["runCount"] == 2
    assert c.delete(f"{API}/assistant/threads/{task['threadId']}", headers=h(ctx, "admin")).status_code == 204
    make_due(ctx, sid)
    fake.say("Yangi suhbatda.")
    assert tick(ctx, sid) == "ok" and one(ctx, "admin", sid)["threadId"] != task["threadId"]


def test_a_task_never_outlives_its_owners_rights_and_a_failing_one_switches_itself_off(ctx: dict, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    wipe(ctx, "reg")
    sid = create(ctx, "reg").json()["id"]

    # the owner's account is switched off: the task stops and says why — no model is asked
    db(c, lambda s: _status(s, ctx["ids"]["reg"], "inactive"))
    c.portal.call(invalidate_principal_cache, ctx["ids"]["reg"])
    make_due(ctx, sid)
    asked = len(fake.calls)
    assert tick(ctx, sid) == "paused" and len(fake.calls) == asked
    db(c, lambda s: _status(s, ctx["ids"]["reg"], "active"))
    c.portal.call(invalidate_principal_cache, ctx["ids"]["reg"])
    task = one(ctx, "reg", sid)
    assert task["isActive"] is False and task["pausedReason"] == runner.MSG_NO_OWNER and task["nextRunAt"] is None and task["lastStatus"] == "failed"
    warn = next(n for n in notices(ctx, "reg") if n["link"] == "/assistant?tasks=1")
    assert warn["kind"] == "warning" and "bajarilmadi" in warn["title"] and "qayta yoqing" in warn["body"]
    # the owner switches it back on: the reason is forgotten, the failures are counted from zero
    on = c.patch(f"{API}/assistant/schedules/{sid}", json={"isActive": True}, headers=h(ctx, "reg")).json()
    assert on["isActive"] and on["pausedReason"] is None and on["nextRunAt"]

    class Broken(FakeProvider):
        async def complete(self, **kw: Any):
            self.calls.append(kw)
            raise ExternalServiceError("AI provayder javob bermadi", code="ai_provider")

    monkeypatch.setattr(service, "build_provider", lambda cfg: Broken())
    for n in (1, 2):
        make_due(ctx, sid)
        assert tick(ctx, sid) == "failed"
        task = one(ctx, "reg", sid)
        assert task["isActive"] and task["lastStatus"] == "failed" and task["lastError"] == "AI provayder javob bermadi" and task["nextRunAt"] and task["runCount"] == 0, n
    make_due(ctx, sid)
    assert tick(ctx, sid) == "paused"
    task = one(ctx, "reg", sid)
    assert task["isActive"] is False and task["pausedReason"].startswith(f"{runner.MAX_FAILS} marta ketma-ket") and task["nextRunAt"] is None
    # a failed run leaves nothing behind in the conversation — its transaction is gone as a whole
    assert task["threadId"] is None and not [t for t in c.get(f"{API}/assistant/threads", headers=h(ctx, "reg")).json() if t["scheduleId"] == sid]


async def _status(s, employee_id: str, status: str) -> None:
    (await s.get(Employee, uuid.UUID(employee_id))).status = status


def test_run_now_and_a_task_for_one_time(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    wipe(ctx, "admin")
    tomorrow = (datetime.now(TASHKENT) + timedelta(days=1)).date().isoformat()
    task = create(ctx, "admin", kind="once", runDate=tomorrow, time="09:00", title="T-bir marta").json()
    assert task["when"].startswith("Bir marta: ") and task["finished"] is False
    # "run now" is the same read-only run; the appointed time does not move and nothing is marked unread
    fake.say("Sinov javobi.")
    r = c.post(f"{API}/assistant/schedules/{task['id']}/run", headers=h(ctx, "admin"))
    assert r.status_code == 200, r.text
    ran = r.json()
    assert ran["lastStatus"] == "ok" and ran["runCount"] == 1 and ran["isActive"] and ran["nextRunAt"] == task["nextRunAt"] and ran["threadId"]
    assert all(tl.REGISTRY[n].kind == "read" for n in fake.calls[-1]["tools"])
    assert next(t for t in c.get(f"{API}/assistant/threads", headers=h(ctx, "admin")).json() if t["id"] == ran["threadId"])["unread"] is False
    # its time comes: it runs once and is finished — nothing ahead, and not "paused by the system"
    make_due(ctx, task["id"])
    fake.say("Eslatma: bugun hisobot kuni.")
    assert tick(ctx, task["id"]) == "ok"
    done = one(ctx, "admin", task["id"])
    assert done["finished"] is True and done["isActive"] is False and done["nextRunAt"] is None and done["pausedReason"] is None and done["runCount"] == 2
    # switching a finished one back on needs a moment that is still ahead
    url = f"{API}/assistant/schedules/{task['id']}"
    assert c.patch(url, json={"isActive": True, "runDate": "2020-01-01"}, headers=h(ctx, "admin")).json()["error"]["code"] == "schedule_past"
    again = c.patch(url, json={"isActive": True, "runDate": tomorrow}, headers=h(ctx, "admin")).json()
    assert again["isActive"] and again["nextRunAt"] and again["finished"] is False


def test_the_assistant_makes_a_task_from_a_sentence_and_takes_it_back(ctx: dict, fake: FakeProvider) -> None:
    c: TestClient = ctx["client"]
    wipe(ctx, "admin")
    tid = new_thread(ctx, "admin", ctx["facility"])
    fake.call("create_schedule", {"title": "T-ertalabki hisobot", "prompt": "Kechagi kun bo‘yicha tushum va cheklar sonini yoz", "kind": "daily", "time": "08:00"})
    act = send(ctx, "admin", tid, "har kuni ertalab 8 da kechagi hisobotni yubor")["pending"]
    rows = {d["label"]: d["value"] for d in act["details"]}
    # the card says where, what and WHEN in words — not five raw fields
    assert act["undoable"] and rows["Qachon"] == "Har kuni 08:00" and rows["Nomi"] == "T-ertalabki hisobot" and f"T-{ctx['tag']}" in rows["Birlik"] and "Vaqt" not in rows and "Turi" not in rows
    assert listed(ctx, "admin") == []
    fake.say("Tayyor.")
    assert c.post(f"{API}/assistant/actions/{act['id']}/confirm", headers=h(ctx, "admin")).status_code == 200
    made = listed(ctx, "admin")
    assert len(made) == 1 and made[0]["when"] == "Har kuni 08:00" and made[0]["unitId"] == ctx["facility"] and made[0]["nextRunAt"]

    # a change through the chat reads as the rule it will become; undoing puts the old rule back
    fake.call("update_schedule", {"scheduleId": made[0]["id"], "kind": "weekly", "weekdays": [1, 2, 3, 4, 5], "time": "09:30"})
    upd = send(ctx, "admin", tid, "faqat ish kunlari 9:30 da bo‘lsin")["pending"]
    rows = {d["label"]: d["value"] for d in upd["details"]}
    assert rows["Qachon"] == "Ish kunlari (dushanba–juma) 09:30" and "T-ertalabki hisobot" in rows["Rejali topshiriq"]
    fake.say("O‘zgartirildi.")
    assert c.post(f"{API}/assistant/actions/{upd['id']}/confirm", headers=h(ctx, "admin")).status_code == 200
    assert listed(ctx, "admin")[0]["when"] == "Ish kunlari (dushanba–juma) 09:30"
    assert c.post(f"{API}/assistant/actions/{upd['id']}/undo", headers=h(ctx, "admin")).status_code == 200
    back = listed(ctx, "admin")[0]
    assert back["when"] == "Har kuni 08:00" and back["weekdays"] == [] and back["isActive"]
    # …and the creation itself goes back too
    assert c.post(f"{API}/assistant/actions/{act['id']}/undo", headers=h(ctx, "admin")).status_code == 200
    assert listed(ctx, "admin") == []
    # the worker's own tick answers with its counters (whatever else is due in this database is not this test's)
    out = c.portal.call(runner.run_due_once)
    assert set(out) == {"due", "ok", "failed", "paused"}
    assert db(c, lambda s: s.execute(select(AssistantSchedule.id).where(AssistantSchedule.employee_id == uuid.UUID(ctx["ids"]["admin"]), AssistantSchedule.deleted_at.is_(None)))).all() == []
