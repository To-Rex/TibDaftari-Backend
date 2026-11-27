"""The training again («o‘quvni takrorlash») against the test database (fixture rows `T-rep-…`).

What must hold: an employee may go through the training again by their own wish — the whole plan as on the first
day, or only the tasks they name — and leave it whenever they like, finding everything as it was; a manager may send
an employee back for the whole plan or for chosen tasks, and that is not the employee's to leave or to shorten; a
plan that begins in the middle of the desk work is given something to begin with.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.exceptions import ValidationError
from app.infrastructure.db.models import AttributeSchema, Category, EmployeeTraining, Patient, ServiceType
from app.modules.training import oversight, samples, tasks
from app.modules.training import service as training_svc
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.helpers import (
    API,
    COUNTRY_ID,
    app_instance,
    bearer,
    db,
    make_employee,
    make_tree,
    phone,
    seed,
    staff_session,
)

RUN = uuid.uuid4().hex[:8]
PW = "secret123"
STARTED = datetime.now(UTC)
RECEPTION = ["patient_create", "order_create", "order_add_item", "payment_take"]


async def _fixture_rows(s) -> dict[str, Any]:
    a = await make_tree(s)
    cat = Category(owner_unit_id=a.region.id, name=f"T-rep Gematologiya {RUN}", sort_order=1)
    schema = AttributeSchema(
        owner_unit_id=a.region.id, name=f"T-rep sxema {RUN}", version=1, status="published",
        fields=[{"key": "hb", "label": "Gemoglobin", "type": "number", "required": True, "order": 1, "decimals": 0, "references": [{"min": 120, "max": 160}]}],
    )  # fmt: skip
    real_patient = Patient(country_id=COUNTRY_ID, company_id=a.facility.company_id, registered_unit_id=a.facility.id, full_name=f"T-rep Haqiqiy Bemor {RUN}", phone=phone("99897"), gender="male", tags=[])
    s.add_all([cat, schema, real_patient])
    await s.flush()
    st1 = ServiceType(owner_unit_id=a.region.id, category_id=cat.id, name=f"T-rep Umumiy qon {RUN}", price=100000, schema_id=schema.id)
    st2 = ServiceType(owner_unit_id=a.region.id, category_id=cat.id, name=f"T-rep Konsultatsiya {RUN}", price=50000)
    s.add_all([st1, st2])
    await s.flush()
    out: dict[str, Any] = {"facility": str(a.facility.id), "st1": str(st1.id), "st2": str(st2.id), "real_patient": str(real_patient.id), "ids": {}, "logins": {}}
    people = {
        # key: (role, in training?)
        "mgr": ("admin", False),
        "old": ("registrator", False),  # works here for years; wants to brush something up
        "pay": ("registrator", False),  # a manager sends them back for one thing
        "reg": ("registrator", True),  # a newcomer: the first, mandatory training
        "boss": ("rahbar", False),  # nothing to practise
        "root": ("superadmin", False),
    }
    for key, (role, trainee) in people.items():
        emp = await make_employee(s, None if key == "root" else a.facility, role, login=f"t-rep-{key}-{RUN}", password=PW, full_name=f"T-rep {key} {RUN}", trainee=trainee, super_admin=key == "root")
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


def _one(env: dict[str, Any], query: str, **params: Any) -> Any:
    return _sql(env, query, **params)[0][0]


def _status(env: dict[str, Any], who: str) -> dict[str, Any]:
    r = env["client"].get(f"{API}/training/status", params={"unitId": env["facility"]}, headers=_h(env, who))
    assert r.status_code == 200, r.text
    return r.json()


def _restart(env: dict[str, Any], who: str, chosen: list[str] | None = None):
    return env["client"].post(f"{API}/training/restart", params={"unitId": env["facility"]}, json=None if chosen is None else {"tasks": chosen}, headers=_h(env, who))


def _leave(env: dict[str, Any], who: str):
    return env["client"].post(f"{API}/training/leave", headers=_h(env, who))


def _reset(env: dict[str, Any], who: str, chosen: list[str] | None = None, reader: str = "mgr"):
    return env["client"].post(f"{API}/employees/{env['ids'][who]}/training/reset", json=None if chosen is None else {"tasks": chosen}, headers=_h(env, reader))


def _card(env: dict[str, Any], who: str) -> dict[str, Any]:
    r = env["client"].get(f"{API}/employees/{env['ids'][who]}", headers=_h(env, "mgr"))
    assert r.status_code == 200, r.text
    return r.json()["training"]


def _keys(out: dict[str, Any]) -> list[str]:
    return [t["key"] for t in out["tasks"]]


def _by_key(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["key"]: t for t in out["tasks"]}


def _passed_notices(env: dict[str, Any], about: str) -> list[dict[str, Any]]:
    got = env["client"].get(f"{API}/notifications", headers=_h(env, "mgr")).json()
    return [n for n in got if n.get("code") == "training.passed" and (n.get("params") or {}).get("employeeId") == env["ids"][about]]


# ----------------------------------------------------------------------------- pure


def test_a_round_is_the_whole_plan_or_what_was_chosen_of_it() -> None:
    assert training_svc.choose(RECEPTION, None) == (RECEPTION, None) == training_svc.choose(RECEPTION, [])
    # a choice keeps the order the tasks are practised in, whatever order it was made in
    assert training_svc.choose(RECEPTION, ["payment_take", "patient_create", "patient_create"]) == (["patient_create", "payment_take"], ["patient_create", "payment_take"])
    assert training_svc.choose(RECEPTION, list(reversed(RECEPTION))) == (RECEPTION, None)  # everything is the whole plan
    for wrong, code in ((["fly"], "unknown_task"), (["result_enter"], "task_not_in_role"), (["patient_create", "result_approve"], "task_not_in_role")):
        with pytest.raises(ValidationError) as e:
            training_svc.choose(RECEPTION, wrong)
        assert e.value.code == code
    assert training_svc._round_tasks(RECEPTION, ["payment_take"]) == ["payment_take"] and training_svc._round_tasks(RECEPTION, None) == RECEPTION
    assert training_svc._round_tasks(["result_enter"], ["payment_take"]) == []  # the role changed: nothing of the choice is left
    # a trainee may ask for both, and nothing else new
    assert tasks.write_allowed("POST", "/training/restart") and tasks.write_allowed("POST", "/training/leave")
    assert not tasks.write_allowed("POST", "/employees/x1/training/reset")


def test_leaving_gives_back_exactly_what_was_before() -> None:
    now = datetime.now(UTC)
    manager = uuid.uuid4()
    row = EmployeeTraining(
        employee_id=uuid.uuid4(), status="skipped", tasks={"patient_create": (now - timedelta(days=40)).isoformat(), "order_create": None}, chosen=None,
        started_at=now - timedelta(days=41), completed_at=now - timedelta(days=39), completed_by=manager, started_by=None, note=None, attempt=1, unavailable=[],
    )  # fmt: skip
    was = training_svc._snapshot(row)
    row.previous = was
    training_svc._begin(row, ["payment_take"], ["payment_take"], row.employee_id)
    row.attempt = 2
    assert (row.status, list(row.tasks), row.chosen, row.completed_at, row.completed_by) == ("active", ["payment_take"], ["payment_take"], None, None)
    training_svc._restore(row)
    assert training_svc._snapshot(row) == was and row.previous is None and row.completed_by == manager and row.attempt == 1
    assert training_svc.colleague_out(row, plan=RECEPTION).state == "skipped"


# ----------------------------------------------------------------------------- by one's own wish


def test_an_employee_repeats_what_they_forgot_and_leaves_when_they_like(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    fac, ids, h = env["facility"], env["ids"], _h(env, "old")
    st = _status(env, "old")
    assert (st["state"], st["active"], st["voluntary"], st["attempt"], st["plan"]) == ("exempt", False, False, 1, RECEPTION)
    real = [p["id"] for p in c.get(f"{API}/units/{fac}/patients", params={"pageSize": 50}, headers=h).json()["items"]]
    assert real == [env["real_patient"]]

    # only how to add a patient
    r = _restart(env, "old", ["patient_create"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["state"], out["active"], out["voluntary"], out["partial"], out["attempt"]) == ("active", True, True, True, 2)
    assert _keys(out) == ["patient_create"] and out["plan"] == RECEPTION and r.headers["x-training"] == "1"
    assert c.get(f"{API}/auth/staff/me", headers=h).json()["training"] is True
    st = _status(env, "old")
    assert _keys(st) == ["patient_create"] and st["total"] == 1  # the role has four tasks; the round stays what was chosen
    # from now on: the sandbox
    assert env["real_patient"] not in [p["id"] for p in c.get(f"{API}/units/{fac}/patients", params={"pageSize": 50}, headers=h).json()["items"]]
    name = f"T-rep Takror Bemor {RUN}"
    assert c.post(f"{API}/units/{fac}/patients", json={"fullName": name, "phone": phone("99893"), "gender": "male"}, headers=h).status_code == 201
    st = _status(env, "old")
    assert (st["done"], st["total"], st["canComplete"]) == (1, 1, True)
    assert _one(env, "SELECT count(*) FROM public.patients WHERE full_name = :n", n=name) == 0 and _one(env, "SELECT count(*) FROM training.patients WHERE full_name = :n", n=name) == 1

    # their own business: a manager sees it, nobody is nagged about it however long it lasts
    _sql(env, "UPDATE employee_training SET started_at = now() - interval '6 days' WHERE employee_id = :e", e=ids["old"])
    card = _card(env, "old")
    assert (card["state"], card["voluntary"], card["partial"], card["attempt"], card["stalled"], card["startedByName"]) == ("active", True, True, 2, False, None)
    assert card["plan"] == RECEPTION and [t["key"] for t in card["tasks"]] == ["patient_create"]
    data = c.get(f"{API}/training/overview", params={"state": "stalled"}, headers=_h(env, "mgr")).json()
    assert ids["old"] not in [i["id"] for i in data["items"]]
    c.portal.call(oversight.remind_once)
    assert _one(env, "SELECT count(*) FROM notifications WHERE code = 'training.stalled' AND params->>'employeeId' = :e", e=ids["old"]) == 0

    # leaving: the real data at once, the training as it was before, nothing left behind
    r = _leave(env, "old")
    assert r.status_code == 200 and (r.json()["state"], r.json()["active"], r.json()["attempt"]) == ("exempt", False, 1) and r.headers["x-training"] == "0", r.text
    assert _sql(env, "SELECT status, note, attempt, previous IS NULL, chosen IS NULL FROM employee_training WHERE employee_id = :e", e=ids["old"]) == [("exempt", "test", 1, True, True)]
    assert [p["id"] for p in c.get(f"{API}/units/{fac}/patients", params={"pageSize": 50}, headers=h).json()["items"]] == [env["real_patient"]]
    assert _one(env, "SELECT count(*) FROM training.patients WHERE created_by = :e", e=ids["old"]) == 0
    assert _leave(env, "old").json()["state"] == "exempt"  # nothing to leave: a no-op
    assert _one(env, "SELECT count(*) FROM public.audit_log WHERE entity_id = :e AND action IN ('training.restart', 'training.leave')", e=ids["old"]) == 2


def test_the_whole_plan_again_and_finishing_it(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    fac, ids, h = env["facility"], env["ids"], _h(env, "old")
    # as on the first day: every task of the role
    out = _restart(env, "old").json()
    assert (_keys(out), out["partial"], out["voluntary"], out["attempt"]) == (RECEPTION, False, True, 2)
    # changing one's mind inside the repeat: another choice — and still ONE repeat, with the same way back
    out = _restart(env, "old", ["payment_take", "order_create"]).json()
    assert (_keys(out), out["partial"], out["voluntary"], out["attempt"]) == (["order_create", "payment_take"], True, True, 2)
    assert _one(env, "SELECT previous->>'status' FROM employee_training WHERE employee_id = :e", e=ids["old"]) == "exempt"

    t = _by_key(_status(env, "old"))
    patient = t["order_create"]["target"]["patientId"]  # a sample patient was put there for them
    r = c.post(f"{API}/units/{fac}/orders", json={"patientId": patient, "serviceTypeIds": [env["st2"]]}, headers=h)
    assert r.status_code == 201, r.text
    order = r.json()["order"]
    assert c.post(f"{API}/training/complete", params={"unitId": fac}, headers=h).status_code == 409  # one of two
    assert _by_key(_status(env, "old"))["payment_take"]["target"]["orderId"] == order["id"]
    assert c.post(f"{API}/orders/{order['id']}/pay", json={"amount": order["total"], "method": "cash"}, headers=h).status_code == 200
    r = c.post(f"{API}/training/complete", params={"unitId": fac}, headers=h)
    assert r.status_code == 200 and (r.json()["state"], r.json()["active"], r.json()["voluntary"], r.json()["attempt"]) == ("passed", False, False, 2), r.text
    card = _card(env, "old")
    assert (card["state"], card["done"], card["total"], card["partial"], card["attempt"], card["voluntary"]) == ("passed", 2, 2, True, 2, False)
    assert _one(env, "SELECT count(*) FROM public.orders WHERE unit_id = :u", u=fac) == 0  # it was practice
    assert _passed_notices(env, "old") == []  # nobody is told about a repeat of one's own
    assert _leave(env, "old").json()["state"] == "passed"  # finished: there is nothing to go back to


def test_what_cannot_be_repeated_is_refused(env: dict[str, Any]) -> None:
    for chosen, code in ((["fly"], "unknown_task"), (["result_enter"], "task_not_in_role")):
        r = _restart(env, "old", chosen)
        assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text
    assert _status(env, "old")["active"] is False  # a refused request starts nothing
    for who in ("boss", "root"):  # a role with nothing to practise; a superadmin
        r = _restart(env, who)
        assert r.status_code == 422 and r.json()["error"]["code"] == "no_tasks", r.text
        assert _status(env, who)["plan"] == []


# ----------------------------------------------------------------------------- a mandatory training


def test_a_mandatory_training_is_not_the_employees_to_leave_or_to_shorten(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    fac, ids, h = env["facility"], env["ids"], _h(env, "reg")
    st = _status(env, "reg")
    assert (st["active"], st["voluntary"], st["attempt"], _keys(st)) == (True, False, 1, RECEPTION)
    name = f"T-rep Yangi Bemor {RUN}"
    assert c.post(f"{API}/units/{fac}/patients", json={"fullName": name, "phone": phone("99893"), "gender": "female"}, headers=h).status_code == 201
    assert _status(env, "reg")["done"] == 1

    r = _leave(env, "reg")
    assert r.status_code == 409 and r.json()["error"]["code"] == "training_mandatory", r.text
    # «start again» is theirs — the same tasks from the beginning, on a clean desk; choosing fewer is not
    r = _restart(env, "reg", ["patient_create"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert (_keys(out), out["done"], out["voluntary"], out["partial"], out["attempt"], out["active"]) == (RECEPTION, 0, False, False, 1, True)
    assert _one(env, "SELECT count(*) FROM training.patients WHERE created_by = :e AND full_name = :n", e=ids["reg"], n=name) == 0
    assert _one(env, "SELECT previous IS NULL FROM employee_training WHERE employee_id = :e", e=ids["reg"]) is True
    assert _leave(env, "reg").status_code == 409


# ----------------------------------------------------------------------------- a manager's choice


def test_a_manager_sends_back_for_chosen_tasks_and_a_cheque_is_waiting(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    fac, ids, h = env["facility"], env["ids"], _h(env, "pay")
    assert _status(env, "pay")["active"] is False
    for chosen, code in ((["fly"], "unknown_task"), (["result_approve"], "task_not_in_role")):
        r = _reset(env, "pay", chosen)
        assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text

    # only how to take a payment
    r = _reset(env, "pay", ["payment_take"])
    assert r.status_code == 200, r.text
    tr = r.json()["training"]
    assert (tr["state"], tr["partial"], tr["voluntary"], tr["attempt"], tr["startedByName"], tr["plan"]) == ("active", True, False, 2, f"T-rep mgr {RUN}", RECEPTION)
    assert [t["key"] for t in tr["tasks"]] == ["payment_take"]
    st = _status(env, "pay")
    assert (_keys(st), st["voluntary"], st["partial"]) == (["payment_take"], False, True)
    # nothing of the desk work before it is in the plan, so a cheque with something to pay was put there
    order_id = _by_key(st)["payment_take"]["target"]["orderId"]
    order = c.get(f"{API}/orders/{order_id}", headers=h).json()["order"]
    assert order["payment"] == "unpaid" and order["total"] > 0 and order["note"] == samples.SAMPLE_NOTE and order["createdByEmployeeId"] == ids["pay"]
    assert _by_key(_status(env, "pay"))["payment_take"]["target"]["orderId"] == order_id  # (one cheque, not one per look)
    assert _one(env, "SELECT count(*) FROM training.orders WHERE created_by_employee_id = :e", e=ids["pay"]) == 1
    assert _leave(env, "pay").status_code == 409  # a manager's decision

    assert c.post(f"{API}/orders/{order_id}/pay", json={"amount": order["total"], "method": "cash"}, headers=h).status_code == 200
    r = c.post(f"{API}/training/complete", params={"unitId": fac}, headers=h)
    assert r.status_code == 200 and r.json()["state"] == "passed", r.text
    assert len(_passed_notices(env, "pay")) == 1  # a mandatory round: those who answer for them are told

    # only how to add a service: an open cheque is waiting
    assert _reset(env, "pay", ["order_add_item"]).json()["training"]["attempt"] == 3
    target = _by_key(_status(env, "pay"))["order_add_item"]["target"]
    order = c.get(f"{API}/orders/{target['orderId']}", headers=h).json()["order"]
    assert order["itemCount"] == 0 and order["status"] != "cancelled"
    assert c.post(f"{API}/orders/{order['id']}/items", json={"serviceTypeIds": [env["st2"]]}, headers=h).status_code == 200
    assert _status(env, "pay")["canComplete"] is True

    # the whole plan, as on the first day (no body — as before)
    tr = _reset(env, "pay").json()["training"]
    assert ([t["key"] for t in tr["tasks"]], tr["partial"], tr["attempt"], tr["done"]) == (RECEPTION, False, 4, 0)
    assert _one(env, "SELECT count(*) FROM training.orders WHERE created_by_employee_id = :e", e=ids["pay"]) == 0  # a clean desk
    assert c.post(f"{API}/employees/{ids['pay']}/training/skip", headers=_h(env, "mgr")).json()["training"]["state"] == "skipped"


def test_a_manager_ending_someones_own_repeat_gives_back_what_was(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    ids = env["ids"]
    assert _restart(env, "old", ["patient_create"]).json()["voluntary"] is True
    r = c.post(f"{API}/employees/{ids['old']}/training/skip", headers=_h(env, "mgr"))
    assert r.status_code == 200, r.text
    tr = r.json()["training"]
    # not «let through without the training»: they had passed before — and that is what stands again
    assert (tr["state"], tr["voluntary"], tr["attempt"], tr["done"], tr["total"]) == ("passed", False, 2, 2, 2)
    assert _status(env, "old")["active"] is False
