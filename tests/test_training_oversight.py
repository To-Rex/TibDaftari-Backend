"""The training seen from above («o‘quv nazorati») against the test database (fixture rows `T-ovr-…`).

What must hold: a manager sees WHICH tasks a colleague has done and which not — the same picture the employee
sees; one list shows everyone the training concerns inside the caller's scope (a superadmin: everywhere); the
people who answer for an employee are told when they finish and when they are stuck; and the employee's own
checklist leads to the very record each remaining task is done on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.config import settings
from app.infrastructure.db.models import AttributeSchema, Category, EmployeeTraining, ServiceType
from app.modules.training import oversight, samples
from app.modules.training import service as training_svc
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.helpers import API, app_instance, bearer, db, make_employee, make_tree, phone, seed, staff_session

RUN = uuid.uuid4().hex[:8]
PW = "secret123"
STARTED = datetime.now(UTC)
RECEPTION = ["patient_create", "order_create", "order_add_item", "payment_take"]


async def _fixture_rows(s) -> dict[str, Any]:
    a = await make_tree(s)
    b = await make_tree(s)  # somebody else's clinic
    cat = Category(owner_unit_id=a.region.id, name=f"T-ovr Gematologiya {RUN}", sort_order=1)
    empty = Category(owner_unit_id=a.region.id, name=f"T-ovr Bo‘sh bo‘lim {RUN}", sort_order=2)
    schema = AttributeSchema(
        owner_unit_id=a.region.id, name=f"T-ovr sxema {RUN}", version=1, status="published",
        fields=[{"key": "hb", "label": "Gemoglobin", "type": "number", "required": True, "order": 1, "decimals": 0, "references": [{"min": 120, "max": 160}]}],
    )  # fmt: skip
    s.add_all([cat, empty, schema])
    await s.flush()
    st1 = ServiceType(owner_unit_id=a.region.id, category_id=cat.id, name=f"T-ovr Umumiy qon {RUN}", price=100000, schema_id=schema.id)
    st2 = ServiceType(owner_unit_id=a.region.id, category_id=cat.id, name=f"T-ovr Konsultatsiya {RUN}", price=50000)
    s.add_all([st1, st2])
    await s.flush()
    out: dict[str, Any] = {"facility": str(a.facility.id), "region": str(a.region.id), "st1": str(st1.id), "st2": str(st2.id), "ids": {}, "logins": {}}
    people = {
        # key: (role, home, in training?, more)
        "mgr": ("admin", a.facility, False, {}),
        "regmgr": ("admin", a.region, False, {}),  # a manager one level up answers for the clinic too
        "boss": ("rahbar", a.facility, False, {}),  # looks at everything, manages nobody
        "old": ("registrator", a.facility, False, {}),
        "outsider": ("admin", b.facility, False, {}),
        "root": ("superadmin", None, False, {"super_admin": True}),
        "reg": ("registrator", a.facility, True, {}),
        "lab": ("laborant", a.facility, True, {"category_ids": [empty.id]}),  # may work only where there is nothing to do
        "ghost": ("registrator", a.facility, True, {}),  # never signs in
        "ghostboss": ("rahbar", a.facility, True, {}),  # never signs in, and has nothing to practise
    }
    for key, (role, home, trainee, more) in people.items():
        emp = await make_employee(s, home, role, login=f"t-ovr-{key}-{RUN}", password=PW, full_name=f"T-ovr {key} {RUN}", trainee=trainee, **more)
        out["ids"][key], out["logins"][key] = str(emp.id), emp.login
    return out


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    data = seed(_fixture_rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["h"] = {}
        yield data

        async def _tidy(s) -> None:
            for emp_id in data["ids"].values():
                await samples.purge(s, uuid.UUID(emp_id))
            # the reminder tells every superadmin of the database, not only this module's
            await s.execute(text("UPDATE notifications SET deleted_at = now() WHERE code LIKE 'training.%' AND created_at >= :t AND deleted_at IS NULL"), {"t": STARTED})

        db(client, _tidy)


def _h(env: dict[str, Any], who: str) -> dict[str, str]:
    if who not in env["h"]:
        env["h"][who] = bearer(staff_session(env["client"], env["logins"][who], PW)["accessToken"])
    return env["h"][who]


def _sql(env: dict[str, Any], query: str, **params: Any) -> list[tuple]:
    async def _run(s) -> list[tuple]:
        result = await s.execute(text(query), params)
        return [tuple(r) for r in result.all()] if result.returns_rows else []

    return db(env["client"], _run)


def _status(env: dict[str, Any], who: str) -> dict[str, Any]:
    r = env["client"].get(f"{API}/training/status", params={"unitId": env["facility"]}, headers=_h(env, who))
    assert r.status_code == 200, r.text
    return r.json()


def _by_key(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["key"]: t for t in status["tasks"]}


def _card(env: dict[str, Any], who: str, reader: str = "mgr") -> dict[str, Any]:
    r = env["client"].get(f"{API}/employees/{env['ids'][who]}", headers=_h(env, reader))
    assert r.status_code == 200, r.text
    return r.json()["training"]


def _overview(env: dict[str, Any], reader: str = "mgr", **params: Any) -> dict[str, Any]:
    r = env["client"].get(f"{API}/training/overview", params=params, headers=_h(env, reader))
    assert r.status_code == 200, r.text
    return r.json()


def _notices(env: dict[str, Any], who: str, code: str) -> list[dict[str, Any]]:
    r = env["client"].get(f"{API}/notifications", headers=_h(env, who))
    assert r.status_code == 200, r.text
    return [n for n in r.json() if n.get("code") == code]


def _about(notices: list[dict[str, Any]], env: dict[str, Any], who: str) -> list[dict[str, Any]]:
    return [n for n in notices if (n.get("params") or {}).get("employeeId") == env["ids"][who]]


# ----------------------------------------------------------------------------- pure


def test_a_colleagues_view_is_built_from_their_row(monkeypatch: pytest.MonkeyPatch) -> None:
    coming = training_svc.colleague_out(None, plan=["patient_create", "order_create"])
    assert coming.state == "pending" and coming.total == 2 and coming.done == 0 and not coming.can_complete
    assert coming.plan == ["patient_create", "order_create"] and coming.attempt == 1 and not coming.voluntary
    assert [(t.key, t.done, t.available) for t in coming.tasks] == [("patient_create", False, True), ("order_create", False, True)]
    assert training_svc.colleague_out(None).state == "exempt"  # nothing to practise: the first sign-in starts nothing

    now = datetime.now(UTC)
    done_at = (now - timedelta(hours=2)).isoformat()
    half = EmployeeTraining(employee_id=uuid.uuid4(), status="active", tasks={"order_create": None, "patient_create": done_at}, unavailable=[], started_at=now - timedelta(days=1))
    out = training_svc.colleague_out(half)
    assert (out.done, out.total, out.can_complete, out.stalled) == (1, 2, False, False)
    assert [t.key for t in out.tasks] == ["patient_create", "order_create"]  # the order they are practised in
    assert out.last_task_at is not None and out.last_task_at.isoformat() == done_at

    # nothing to practise on where they work: no task holds them back — only «finish» is left
    stuck = EmployeeTraining(employee_id=uuid.uuid4(), status="active", tasks={"result_enter": None, "result_submit": None}, unavailable=["result_enter", "result_submit"], started_at=now - timedelta(days=10))
    out = training_svc.colleague_out(stuck)
    assert out.can_complete and out.stalled and out.done == 0 and all(not t.available for t in out.tasks)
    monkeypatch.setattr(settings, "training_stall_days", 0)  # the reminder switched off: nobody counts as stuck
    assert training_svc.colleague_out(stuck).stalled is False and training_svc.stall_cutoff() is None

    # a finished training keeps its marks; what could not be practised then no longer matters
    over = EmployeeTraining(employee_id=uuid.uuid4(), status="passed", tasks={"patient_create": done_at}, unavailable=["patient_create"], started_at=now - timedelta(days=30), completed_at=now)
    out = training_svc.colleague_out(over, by_name="Rahbar")
    assert (out.state, out.done, out.total, out.can_complete, out.stalled, out.completed_by_name) == ("passed", 1, 1, False, False, "Rahbar")


# ----------------------------------------------------------------------------- the employee: where to continue


def test_the_checklist_leads_to_the_very_record(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    fac, h = env["facility"], _h(env, "reg")
    _status(env, "reg")  # (the first look puts the sample patients there)
    t = _by_key(_status(env, "reg"))
    assert t["patient_create"]["target"] is None  # a form, not a record
    sample = t["order_create"]["target"]["patientId"]
    names = {p["id"]: p["fullName"] for p in c.get(f"{API}/units/{fac}/patients", params={"pageSize": 50}, headers=h).json()["items"]}
    assert names[sample] in {n for n, _, _ in samples.SAMPLE_PATIENTS}
    # no cheque yet: the later tasks start at the patient too
    assert t["order_add_item"]["target"] == {"patientId": sample, "orderId": None, "itemId": None} == t["payment_take"]["target"]

    r = c.post(f"{API}/units/{fac}/patients", json={"fullName": f"T-ovr Mashq Bemor {RUN}", "phone": phone("99893"), "gender": "female"}, headers=h)
    assert r.status_code == 201, r.text
    mine = r.json()["id"]
    t = _by_key(_status(env, "reg"))
    assert t["patient_create"]["done"] is True and t["patient_create"]["target"] is None
    assert t["order_create"]["target"]["patientId"] == mine  # what they made themselves comes before the samples

    r = c.post(f"{API}/units/{fac}/orders", json={"patientId": mine, "serviceTypeIds": []}, headers=h)
    assert r.status_code == 201, r.text
    order = r.json()["order"]["id"]
    t = _by_key(_status(env, "reg"))
    assert t["order_create"]["done"] is True and t["order_create"]["target"] is None
    assert t["order_add_item"]["target"] == {"patientId": None, "orderId": order, "itemId": None}
    assert t["payment_take"]["target"]["orderId"] == order  # nothing to pay yet: the cheque a service is added to first

    r = c.post(f"{API}/orders/{order}/items", json={"serviceTypeIds": [env["st2"]]}, headers=h)
    assert r.status_code == 200, r.text
    st = _status(env, "reg")
    t = _by_key(st)
    assert st["done"] == 3 and t["payment_take"]["target"]["orderId"] == order and t["order_add_item"]["target"] is None
    assert c.get(f"{API}/orders/{order}", headers=h).status_code == 200  # the target is theirs to open …
    assert c.get(f"{API}/orders/{order}", headers=_h(env, "old")).status_code == 404  # … and exists nowhere else
    env["order"] = order


# ----------------------------------------------------------------------------- a manager: which tasks


def test_a_manager_sees_which_tasks_are_done_and_which_not(env: dict[str, Any]) -> None:
    card = _card(env, "reg")
    assert (card["state"], card["done"], card["total"], card["canComplete"], card["stalled"]) == ("active", 3, 4, False, False)
    assert [(t["key"], t["done"]) for t in card["tasks"]] == [("patient_create", True), ("order_create", True), ("order_add_item", True), ("payment_take", False)]
    assert all(bool(t["doneAt"]) == t["done"] and t["available"] for t in card["tasks"]) and card["lastTaskAt"] == card["tasks"][2]["doneAt"]
    assert all(t["target"] is None for t in card["tasks"])  # sandbox ids mean nothing to anyone else

    # not signed in yet: the tasks the first sign-in will give — or nothing to practise at all
    ghost = _card(env, "ghost")
    assert (ghost["state"], ghost["done"], ghost["total"], ghost["startedAt"]) == ("pending", 0, 4, None) and [t["key"] for t in ghost["tasks"]] == RECEPTION
    nothing = _card(env, "ghostboss")
    assert (nothing["state"], nothing["total"], nothing["tasks"], nothing["startedAt"]) == ("exempt", 0, [], None)

    # where there is nothing to practise on, the employee is told so — and the manager sees the same picture
    st = _status(env, "lab")
    assert st["total"] == 2 and all(not t["available"] and t["target"] is None for t in st["tasks"]) and st["canComplete"] is True
    lab = _card(env, "lab")
    assert (lab["done"], lab["total"], lab["canComplete"]) == (0, 2, True) and all(not t["available"] and not t["done"] for t in lab["tasks"])


# ----------------------------------------------------------------------------- the overview


def test_one_list_shows_everyone_the_training_concerns(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    ids = env["ids"]
    data = _overview(env)  # default: everyone it still concerns
    assert {i["id"] for i in data["items"]} == {ids["reg"], ids["lab"], ids["ghost"]} and data["total"] == 3
    assert data["counts"] == {"active": 2, "stalled": 0, "ready": 1, "pending": 1, "passed": 0, "skipped": 0}
    assert data["stallDays"] == settings.training_stall_days == 3
    assert [i["id"] for i in data["items"]][-1] == ids["ghost"]  # those in the training first, longest first
    row = next(i for i in data["items"] if i["id"] == ids["reg"])
    assert row["fullName"] == f"T-ovr reg {RUN}" and row["login"] == env["logins"]["reg"] and row["unitId"] == env["facility"] and row["unitName"] and row["roleName"]
    assert row["training"]["done"] == 3 and [t["key"] for t in row["training"]["tasks"] if not t["done"]] == ["payment_take"]

    assert [i["id"] for i in _overview(env, state="pending")["items"]] == [ids["ghost"]]
    assert [i["id"] for i in _overview(env, state="ready")["items"]] == [ids["lab"]]
    assert {i["id"] for i in _overview(env, state="active")["items"]} == {ids["reg"], ids["lab"]}
    assert _overview(env, state="passed")["items"] == [] and _overview(env, state="all")["total"] == 3
    found = _overview(env, search=env["logins"]["ghost"].upper())
    assert [i["id"] for i in found["items"]] == [ids["ghost"]] and found["counts"]["pending"] == 1 and found["counts"]["active"] == 0
    assert [i["id"] for i in _overview(env, sortBy="progress", sortDir="desc", state="active")["items"]] == [ids["reg"], ids["lab"]]
    paged = _overview(env, pageSize=1, page=2)
    assert len(paged["items"]) == 1 and paged["totalPages"] == 3 and paged["page"] == 2

    # the scope: one's own units, narrowed on request; a superadmin — every company and clinic at once
    assert {i["id"] for i in _overview(env, unitId=env["facility"])["items"]} == {ids["reg"], ids["lab"], ids["ghost"]}
    assert c.get(f"{API}/training/overview", params={"unitId": env["region"]}, headers=_h(env, "mgr")).status_code == 403
    assert _overview(env, "regmgr", unitId=env["region"])["total"] == 3 and _overview(env, "regmgr")["total"] == 3
    assert _overview(env, "outsider", search=RUN)["total"] == 0
    everywhere = _overview(env, "root", search=RUN, state="all")
    assert {i["id"] for i in everywhere["items"]} == {ids["reg"], ids["lab"], ids["ghost"]}
    assert _overview(env, "root")["total"] >= 3
    assert not any(i["id"] == ids["root"] for i in _overview(env, "root", search=RUN, state="all")["items"])
    # only someone who may look at employees
    assert c.get(f"{API}/training/overview", headers=_h(env, "old")).status_code == 403
    assert c.get(f"{API}/training/overview", headers=_h(env, "reg")).status_code == 403


# ----------------------------------------------------------------------------- telling the managers


def test_the_managers_are_told_when_someone_is_stuck(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = env["client"]
    ids = env["ids"]
    remind = lambda: c.portal.call(oversight.remind_once)  # noqa: E731
    remind()  # (whatever older runs left in the test database is mentioned now, not in the middle of this test)
    assert _about(_notices(env, "mgr", "training.stalled"), env, "reg") == []  # a day in the training is not «stuck»

    _sql(env, "UPDATE employee_training SET started_at = now() - interval '5 days' WHERE employee_id = :e", e=ids["reg"])
    data = _overview(env, state="stalled")
    assert [i["id"] for i in data["items"]] == [ids["reg"]] and data["items"][0]["training"]["stalled"] is True and data["counts"]["stalled"] == 1
    assert _card(env, "reg")["stalled"] is True

    assert remind() >= 1
    told = _about(_notices(env, "mgr", "training.stalled"), env, "reg")
    assert len(told) == 1
    n = told[0]
    assert n["params"] == {"employeeId": ids["reg"], "name": f"T-ovr reg {RUN}", "unit": n["params"]["unit"], "days": 5, "done": 3, "total": 4} and n["params"]["unit"]
    assert n["link"] == f"/admin/employees/{ids['reg']}" and n["kind"] == "warning" and n["read"] is False
    assert n["title"] == "O‘quv tugatilmagan" and f"T-ovr reg {RUN}" in n["body"] and "5 kundan beri" in n["body"] and "3/4" in n["body"]
    # everyone who answers for them — the manager above and the superadmin too; nobody else
    assert len(_about(_notices(env, "regmgr", "training.stalled"), env, "reg")) == 1
    assert len(_about(_notices(env, "root", "training.stalled"), env, "reg")) == 1
    for bystander in ("boss", "old", "outsider"):
        assert _about(_notices(env, bystander, "training.stalled"), env, "reg") == []
    assert _sql(env, "SELECT count(*) FROM public.notifications WHERE employee_id = :e AND code LIKE 'training.%'", e=ids["reg"])[0][0] == 0
    assert _sql(env, "SELECT count(*) FROM employee_training WHERE employee_id = :e AND reminded_at IS NOT NULL", e=ids["lab"])[0][0] == 0  # not stuck yet

    # once is enough for a week …
    remind()
    assert len(_about(_notices(env, "mgr", "training.stalled"), env, "reg")) == 1
    # … then they are mentioned again, for as long as it lasts
    _sql(env, "UPDATE employee_training SET reminded_at = now() - interval '8 days', started_at = now() - interval '13 days' WHERE employee_id = :e", e=ids["reg"])
    remind()
    again = _about(_notices(env, "mgr", "training.stalled"), env, "reg")
    assert len(again) == 2 and sorted(n["params"]["days"] for n in again) == [5, 13]

    # the reminder switched off
    _sql(env, "UPDATE employee_training SET reminded_at = NULL WHERE employee_id = :e", e=ids["reg"])
    monkeypatch.setattr(settings, "training_stall_days", 0)
    assert remind() == 0 and len(_about(_notices(env, "mgr", "training.stalled"), env, "reg")) == 2
    monkeypatch.undo()
    _sql(env, "UPDATE employee_training SET reminded_at = now() WHERE employee_id = :e", e=ids["reg"])


def test_many_stuck_people_make_one_notice(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]

    async def _crowd(s) -> list[str]:
        from app.infrastructure.db.models import OrgUnit

        facility = await s.get(OrgUnit, uuid.UUID(env["facility"]))
        made = []
        for i in range(oversight.DIGEST_FROM):
            emp = await make_employee(s, facility, "registrator", login=f"t-ovr-crowd{i}-{RUN}", password=PW, full_name=f"T-ovr crowd{i} {RUN}", trainee=True)
            s.add(EmployeeTraining(employee_id=emp.id, status="active", tasks={k: None for k in RECEPTION}, started_at=datetime.now(UTC) - timedelta(days=10)))
            made.append(str(emp.id))
        return made

    crowd = db(c, _crowd)
    before = len(_notices(env, "mgr", "training.stalled"))
    assert c.portal.call(oversight.remind_once) >= oversight.DIGEST_FROM
    digest = _notices(env, "mgr", "training.stalled_many")
    assert len(digest) == 1 and digest[0]["params"] == {"count": oversight.DIGEST_FROM, "days": 3} and digest[0]["link"] == "/admin/training?state=stalled"
    assert digest[0]["kind"] == "warning" and str(oversight.DIGEST_FROM) in digest[0]["body"]
    assert len(_notices(env, "mgr", "training.stalled")) == before  # not one notice per person on top of it
    assert _sql(env, "SELECT count(*) FROM employee_training WHERE employee_id = ANY(CAST(:ids AS uuid[])) AND reminded_at IS NOT NULL", ids=crowd)[0][0] == oversight.DIGEST_FROM
    assert _overview(env)["counts"]["stalled"] == oversight.DIGEST_FROM + 1
    _sql(env, "UPDATE employees SET deleted_at = now() WHERE id = ANY(CAST(:ids AS uuid[]))", ids=crowd)  # the crowd leaves


def test_finishing_is_announced_to_those_who_answer_for_the_employee(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    ids, h = env["ids"], _h(env, "reg")
    r = c.post(f"{API}/orders/{env['order']}/pay", json={"amount": 50000, "method": "cash"}, headers=h)
    assert r.status_code == 200, r.text
    assert _card(env, "reg")["canComplete"] is True and _overview(env)["counts"]["ready"] == 2
    assert _about(_notices(env, "mgr", "training.passed"), env, "reg") == []

    r = c.post(f"{API}/training/complete", params={"unitId": env["facility"]}, headers=h)
    assert r.status_code == 200 and r.json()["state"] == "passed", r.text
    for reader in ("mgr", "regmgr", "root"):
        told = _about(_notices(env, reader, "training.passed"), env, "reg")
        assert len(told) == 1, reader
    n = _about(_notices(env, "mgr", "training.passed"), env, "reg")[0]
    assert n["kind"] == "success" and n["link"] == f"/admin/employees/{ids['reg']}" and n["title"] == "O‘quv yakunlandi" and f"T-ovr reg {RUN}" in n["body"]
    assert n["params"] | {"unit": "", "days": 0} == {"employeeId": ids["reg"], "name": f"T-ovr reg {RUN}", "unit": "", "done": 4, "total": 4, "days": 0}
    for bystander in ("boss", "old", "outsider"):
        assert _about(_notices(env, bystander, "training.passed"), env, "reg") == []
    assert c.get(f"{API}/notifications", headers=h).json() == []  # nothing about themselves, in either world

    # a notice is read like any other
    assert c.post(f"{API}/notifications/read", json={"id": n["id"]}, headers=_h(env, "mgr")).status_code == 200
    assert _about(_notices(env, "mgr", "training.passed"), env, "reg")[0]["read"] is True
    assert _about(_notices(env, "regmgr", "training.passed"), env, "reg")[0]["read"] is False

    # the overview keeps the history
    done = _overview(env, state="passed")
    assert [i["id"] for i in done["items"]] == [ids["reg"]] and done["counts"]["passed"] == 1 and done["counts"]["active"] == 1
    card = done["items"][0]["training"]
    assert card["state"] == "passed" and card["done"] == 4 and all(t["done"] for t in card["tasks"]) and card["completedAt"] and card["stalled"] is False
    # someone let through by a manager is history too — and nobody is told about a manager's own decision
    assert c.post(f"{API}/employees/{ids['lab']}/training/skip", headers=_h(env, "mgr")).status_code == 200
    data = _overview(env, state="skipped")
    assert [i["id"] for i in data["items"]] == [ids["lab"]] and data["items"][0]["training"]["completedByName"] == f"T-ovr mgr {RUN}"
    assert data["counts"] == {"active": 0, "stalled": 0, "ready": 0, "pending": 1, "passed": 1, "skipped": 1}
    assert _about(_notices(env, "regmgr", "training.passed"), env, "lab") == []
