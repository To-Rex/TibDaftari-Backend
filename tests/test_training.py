"""The mandatory training of a new employee («o‘quv rejimi») against the test database (fixture rows `T-trn-…`).

What must hold: someone who has never worked here practises in the sandbox schema — nothing they do reaches a real
table, nothing real reaches them — until every task of their role is done (or a manager lets them through); what
they may not practise is refused; people who already worked here never notice any of it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import settings
from app.infrastructure.db import realm
from app.infrastructure.db.models import AttributeSchema, Category, Patient, ResultTemplate, ServiceType
from app.infrastructure.db.session import session_scope
from app.infrastructure.redis import cache
from app.infrastructure.redis.client import get_redis
from app.modules.training import samples, sandbox, tasks
from app.modules.training import service as training_svc
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

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
    system_role,
)

RUN = uuid.uuid4().hex[:8]
PW = "secret123"
PEOPLE = {
    # key: (role, in training?)
    "mgr": ("admin", False),
    "old": ("registrator", False),
    "reg": ("registrator", True),
    "lab": ("laborant", True),
    "lab2": ("laborant", True),
    "doc": ("vrach", True),
    "newadmin": ("admin", True),
    "boss": ("rahbar", True),
    "inst": ("installer", True),  # sets clinics up — like a superadmin, never sent to practise
    "ghost": ("registrator", True),  # never signs in
}


async def _fixture_rows(s) -> dict[str, Any]:
    a = await make_tree(s)
    out: dict[str, Any] = {"facility": str(a.facility.id), "region": str(a.region.id), "tag": a.tag, "ids": {}, "logins": {}}
    for key, (role, trainee) in PEOPLE.items():
        home = a.region if role == "installer" else a.facility
        emp = await make_employee(s, home, role, login=f"t-trn-{key}-{RUN}", password=PW, full_name=f"T-trn {key} {RUN}", trainee=trainee)
        out["ids"][key], out["logins"][key] = str(emp.id), emp.login
    real_patient = Patient(country_id=COUNTRY_ID, company_id=a.facility.company_id, registered_unit_id=a.facility.id, full_name=f"T-trn Haqiqiy Bemor {RUN}", phone=phone("99897"), gender="male", tags=[])
    cat = Category(owner_unit_id=a.region.id, name=f"T-trn Gematologiya {RUN}", sort_order=1)
    schema = AttributeSchema(
        owner_unit_id=a.region.id,
        name=f"T-trn sxema {RUN}",
        version=1,
        status="published",
        fields=[
            {"key": "hb", "label": "Gemoglobin", "type": "number", "required": True, "order": 1, "decimals": 0, "references": [{"min": 120, "max": 160}]},
            {"key": "rang", "label": "Rangi", "type": "select", "required": True, "order": 2, "options": [{"value": "sariq", "label": "Sariq", "flag": "abnormal"}, {"value": "tiniq", "label": "Tiniq", "flag": "normal"}]},
        ],
    )
    s.add_all([real_patient, cat, schema])
    await s.flush()
    st1 = ServiceType(owner_unit_id=a.region.id, category_id=cat.id, name=f"T-trn Umumiy qon {RUN}", price=100000, schema_id=schema.id)
    st2 = ServiceType(owner_unit_id=a.region.id, category_id=cat.id, name=f"T-trn Konsultatsiya {RUN}", price=50000)
    s.add_all([st1, st2])
    await s.flush()
    s.add(ResultTemplate(owner_unit_id=a.region.id, name=f"T-trn natija {RUN}", status="active", version=1, service_type_ids=[st1.id], category_ids=[], scope="item", doc={"paper": "A4", "elements": []}))
    await s.flush()
    out |= {"real_patient": str(real_patient.id), "st1": str(st1.id), "st2": str(st2.id), "cat": str(cat.id)}
    out["roles"] = {k: str((await system_role(s, k)).id) for k in ("registrator", "rahbar", "vrach")}
    return out


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    data = seed(_fixture_rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["h"] = {}
        yield data

        async def _tidy(s) -> None:  # the sandbox is not part of the suite's own cleanup
            for emp_id in data["ids"].values():
                await samples.purge(s, uuid.UUID(emp_id))

        db(client, _tidy)


def _h(env: dict[str, Any], who: str) -> dict[str, str]:
    """The person's bearer header — they sign in the first time they are needed (that is when a training starts)."""
    if who not in env["h"]:
        env["h"][who] = bearer(staff_session(env["client"], env["logins"][who], PW)["accessToken"])
    return env["h"][who]


def _sql(env: dict[str, Any], query: str, **params: Any) -> list[tuple]:
    async def _run(s) -> list[tuple]:
        return [tuple(r) for r in (await s.execute(text(query), params)).all()]

    return db(env["client"], _run)


def _one(env: dict[str, Any], query: str, **params: Any) -> Any:
    return _sql(env, query, **params)[0][0]


def _status(env: dict[str, Any], who: str) -> dict[str, Any]:
    r = env["client"].get(f"{API}/training/status", params={"unitId": env["facility"]}, headers=_h(env, who))
    assert r.status_code == 200, r.text
    return r.json()


def _tasks(status: dict[str, Any]) -> dict[str, bool]:
    return {t["key"]: t["done"] for t in status["tasks"]}


# ----------------------------------------------------------------------------- pure


def test_task_and_whitelist_rules_cover_real_routes_only() -> None:
    """Every write a trainee may make is a route that exists; nothing administrative is among them."""
    spec = app_instance().openapi()
    ops = [(m.upper(), tasks.api_path(p)) for p, methods in spec["paths"].items() for m in methods if m.upper() not in tasks.SAFE_METHODS]

    def concrete(path: str) -> str:
        import re

        return re.sub(r"\{[^}]+\}", "x1", path)

    allowed = sorted({(m, p) for m, p in ops if tasks.write_allowed(m, concrete(p))})
    assert allowed == sorted(
        [
            ("DELETE", "/orders/{order_id}"),
            ("DELETE", "/orders/{order_id}/items/{item_id}"),
            ("DELETE", "/patients/{patient_id}/portal-access"),
            ("POST", "/auth/logout"),
            ("POST", "/documents/{document_id}/printed"),
            ("POST", "/documents/{document_id}/sms"),
            ("POST", "/items/{item_id}/approve"),
            ("POST", "/items/{item_id}/reject"),
            ("POST", "/items/{item_id}/revoke"),
            ("POST", "/items/{item_id}/submit"),
            ("POST", "/notifications/read"),
            ("POST", "/orders/{order_id}/approve"),
            ("POST", "/orders/{order_id}/cancel"),
            ("POST", "/orders/{order_id}/items"),
            ("POST", "/orders/{order_id}/pay"),
            ("POST", "/payments/{payment_id}/refund"),
            ("POST", "/templates/{template_id}/preview.pdf"),
            ("POST", "/training/complete"),
            ("POST", "/units/{unit_id}/messages/send"),
            ("POST", "/units/{unit_id}/orders"),
            ("POST", "/units/{unit_id}/patients"),
            ("POST", "/units/{unit_id}/patients/duplicates"),
            ("PUT", "/items/{item_id}/values"),
            ("PUT", "/patients/{patient_id}"),
            ("PUT", "/patients/{patient_id}/portal-access"),
            ("PUT", "/payments/{payment_id}/method"),
        ]
    )
    # every pattern of the list stands for a route that is really there (no dead or misspelt entries)
    for method, pattern in tasks.ALLOWED_WRITES:
        assert any(m == method and pattern.match(concrete(p)) for m, p in ops), (method, pattern.pattern)
    # each task is proved by exactly one kind of request, and what proves it is allowed
    for t in tasks.TASKS:
        hits = [(m, p) for m, p in ops if tasks.task_done_by(m, concrete(p)) == t.key]
        assert hits and all(tasks.write_allowed(m, concrete(p)) for m, p in hits), t.key
    assert tasks.task_done_by("GET", "/units/x1/patients") is None and tasks.task_done_by("POST", "/units/x1/patients/duplicates") is None
    # roles → tasks
    assert [t.key for t in tasks.tasks_for({"reception.patient.write", "reception.order.create", "reception.payment.create"})] == ["patient_create", "order_create", "order_add_item", "payment_take"]
    assert [t.key for t in tasks.tasks_for({"lab.result.write", "lab.result.submit"})] == ["result_enter", "result_submit"]
    assert [t.key for t in tasks.tasks_for({"lab.result.write", "confirm.result.approve"})] == ["result_enter", "result_approve"]
    assert tasks.tasks_for({"reports.finance.read", "admin.employee.read"}) == []


def test_sample_values_fill_every_field_inside_its_norm() -> None:
    fields = [
        {"key": "hb", "type": "number", "decimals": 0, "references": [{"gender": "male", "min": 130, "max": 160}]},
        {"key": "ph", "type": "number", "decimals": 1, "min": 4.5, "max": 8},
        {"key": "free", "type": "number"},
        {"key": "rang", "type": "select", "options": [{"value": "qizil", "flag": "critical"}, {"value": "tiniq", "flag": "normal"}]},
        {"key": "multi", "type": "multiselect", "options": [{"value": "a"}, {"value": "b"}]},
        {"key": "ok", "type": "boolean"},
        {"key": "kun", "type": "date"},
        {"key": "izoh", "type": "longtext"},
        {"key": "qisqa", "type": "text"},
        {"key": "tbl", "type": "table", "columns": [{"key": "name", "type": "text"}, {"key": "val", "type": "number", "min": 1, "max": 3}], "presetRows": [{"name": "WBC", "val": ""}]},
        {"key": "empty_tbl", "type": "table", "columns": [{"key": "c", "type": "text"}], "presetRows": []},
        {"type": "text"},
    ]
    v = samples.sample_values(fields)
    assert v["hb"] == 145 and v["ph"] == 6.2 and v["free"] == 1 and v["rang"] == "tiniq" and v["multi"] == ["a"] and v["ok"] is False
    assert len(v["kun"]) == 10 and v["izoh"] and v["qisqa"]
    assert v["tbl"] == [{"name": "WBC", "val": 2}] and v["empty_tbl"] == [{"c": "Me’yorda"}]
    assert set(v) == {f["key"] for f in fields if f.get("key")}
    assert fields[9]["presetRows"] == [{"name": "WBC", "val": ""}]  # the schema itself is not touched


def test_purge_names_only_sandbox_tables() -> None:
    """Whatever session it is given, the clean-up cannot reach a real row: every table carries the sandbox schema."""
    import inspect
    import re

    source = inspect.getsource(samples.purge)
    named = re.findall(r"(?:FROM|UPDATE|JOIN)\s+([{}\w.]+)", source)
    assert named and all(n.startswith("{_S}.") for n in named), named
    assert samples._S == realm.SANDBOX_SCHEMA == "training"


def test_training_cache_keys_never_mix_with_the_real_ones(env: dict[str, Any]) -> None:
    async def _run(_s) -> None:
        r = get_redis()
        key = f"t-trn:probe:{RUN}"
        await cache.set_json(key, {"who": "real"}, 60)
        token = realm.enter(True)
        try:
            assert await cache.get_json(key) is None  # a trainee never reads what a real request computed
            await cache.set_json(key, {"who": "trainee"}, 60)
            assert await cache.get_json(key) == {"who": "trainee"}
        finally:
            realm.leave(token)
        assert await cache.get_json(key) == {"who": "real"}
        assert await r.exists(key) and await r.exists(cache.TRAINING_PREFIX + key)
        await cache.delete(key)  # a real write drops both copies
        assert not await r.exists(key) and not await r.exists(cache.TRAINING_PREFIX + key)
        assert realm.is_training() is False

    db(env["client"], _run)


# ----------------------------------------------------------------------------- the database's own refusal


def test_sandbox_connection_cannot_write_a_real_table(env: dict[str, Any]) -> None:
    async def _run(_s) -> dict[str, Any]:
        seen: dict[str, Any] = {"ready": sandbox.is_ready()}
        async with session_scope(training=True) as t:
            seen["user"] = (await t.execute(text("SELECT current_user"))).scalar_one()
            home = "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = to_regclass(:t)"
            seen["orders"] = (await t.execute(text(home), {"t": "orders"})).scalar_one()
            seen["employees"] = (await t.execute(text(home), {"t": "employees"})).scalar_one()
            seen["reads_real"] = (await t.execute(text("SELECT count(*) FROM employees"))).scalar_one() > 0
            await t.execute(text("UPDATE orders SET note = note WHERE false"))  # its own tables: fine
        for label, stmt in {
            "real employees": "UPDATE employees SET full_name = full_name WHERE false",
            "real orders": "UPDATE public.orders SET note = note WHERE false",
            "real patients": "DELETE FROM public.patients WHERE false",
            "training state": "UPDATE employee_training SET note = note WHERE false",
            "files": "DELETE FROM stored_files WHERE false",
            "lock": "SELECT id FROM employees LIMIT 1 FOR UPDATE",
        }.items():
            try:
                async with session_scope(training=True) as t:
                    await t.execute(text(stmt))
                seen[label] = "allowed"
            except DBAPIError as exc:
                seen[label] = "denied" if "permission denied" in str(exc.orig) else str(exc.orig)
        return seen

    seen = db(env["client"], _run)
    assert seen["ready"] is True and seen["user"] == realm.SANDBOX_ROLE
    assert seen["orders"] == "training" and seen["employees"] == "public" and seen["reads_real"] is True
    refused = ("real employees", "real orders", "real patients", "training state", "files", "lock")
    assert {k: seen[k] for k in refused} == dict.fromkeys(refused, "denied")


# ----------------------------------------------------------------------------- who is in training


def test_only_a_newcomer_with_tasks_starts_the_training(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    reg = staff_session(c, env["logins"]["reg"], PW)
    env["h"]["reg"] = bearer(reg["accessToken"])
    assert reg["training"] is True
    assert staff_session(c, env["logins"]["old"], PW)["training"] is False  # worked here before
    assert staff_session(c, env["logins"]["mgr"], PW)["training"] is False
    assert staff_session(c, env["logins"]["boss"], PW)["training"] is False  # a role with nothing to practise
    assert staff_session(c, env["logins"]["inst"], PW)["training"] is False  # an installer is not held back from the setup work
    assert c.get(f"{API}/auth/staff/me", headers=env["h"]["reg"]).json()["training"] is True
    rows = dict(_sql(env, "SELECT e.login, t.status || ':' || coalesce(t.note, '') || ':' || (SELECT count(*) FROM jsonb_object_keys(t.tasks)) FROM employee_training t JOIN employees e ON e.id = t.employee_id WHERE e.login LIKE :p", p=f"t-trn-%-{RUN}"))
    assert rows[env["logins"]["reg"]] == "active::4" and rows[env["logins"]["boss"]] == "exempt:no_tasks:0" and rows[env["logins"]["old"]] == "exempt:test:0"
    assert rows[env["logins"]["inst"]] == "exempt:installer:0"
    assert env["logins"]["ghost"] not in rows  # nobody has signed in to that account yet

    st = _status(env, "reg")
    assert st["state"] == "active" and st["active"] is True and st["done"] == 0 and st["total"] == 4 and st["canComplete"] is False
    assert [t["key"] for t in st["tasks"]] == ["patient_create", "order_create", "order_add_item", "payment_take"] and all(t["available"] and not t["done"] for t in st["tasks"])
    assert st["unitId"] == env["facility"] and st["unitName"]
    for who in ("old", "boss", "mgr"):
        other = _status(env, who)
        assert other["state"] == "exempt" and other["active"] is False and other["tasks"] == []


# ----------------------------------------------------------------------------- reception


def test_a_reception_trainee_works_only_in_the_sandbox(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    fac, h, h_old = env["facility"], _h(env, "reg"), _h(env, "old")
    _status(env, "reg")  # (the page asks for it first: the sample patients are put there)

    # each side sees only its own world
    mine = c.get(f"{API}/units/{fac}/patients", params={"pageSize": 50}, headers=h).json()
    names = [p["fullName"] for p in mine["items"]]
    assert sorted(names) == sorted(n for n, _, _ in samples.SAMPLE_PATIENTS), names
    real = c.get(f"{API}/units/{fac}/patients", params={"pageSize": 50}, headers=h_old).json()
    assert [p["id"] for p in real["items"]] == [env["real_patient"]]
    assert c.get(f"{API}/patients/{env['real_patient']}", headers=h).status_code == 404

    # a request that fails proves nothing
    assert c.post(f"{API}/units/{fac}/patients", json={"fullName": "", "phone": "1"}, headers=h).status_code == 422
    assert _tasks(_status(env, "reg"))["patient_create"] is False

    name = f"T-trn Mashq Bemor {RUN}"
    r = c.post(f"{API}/units/{fac}/patients", json={"fullName": name, "phone": phone("99893"), "gender": "female"}, headers=h)
    assert r.status_code == 201, r.text
    patient = r.json()
    r = c.post(f"{API}/units/{fac}/orders", json={"patientId": patient["id"], "serviceTypeIds": [env["st1"]]}, headers=h)
    assert r.status_code == 201, r.text
    order = r.json()["order"]
    assert _tasks(_status(env, "reg")) == {"patient_create": True, "order_create": True, "order_add_item": False, "payment_take": False}
    r = c.post(f"{API}/orders/{order['id']}/items", json={"serviceTypeIds": [env["st2"]]}, headers=h)
    assert r.status_code == 200, r.text
    r = c.post(f"{API}/orders/{order['id']}/pay", json={"amount": 150000, "method": "cash", "sendSms": True}, headers=h)
    assert r.status_code == 200 and r.json()["order"]["payment"] == "paid", r.text
    st = _status(env, "reg")
    assert st["done"] == 4 and st["canComplete"] is True and all(t["doneAt"] for t in st["tasks"])
    env["reg_order"], env["reg_patient"] = order["id"], patient["id"]

    # … and none of it is real: the rows live in the sandbox schema only
    assert _one(env, "SELECT count(*) FROM public.patients WHERE full_name = :n", n=name) == 0
    assert _one(env, "SELECT count(*) FROM training.patients WHERE full_name = :n", n=name) == 1
    assert _one(env, "SELECT count(*) FROM public.orders WHERE unit_id = :u", u=fac) == 0
    assert _one(env, "SELECT count(*) FROM training.orders WHERE unit_id = :u AND created_by_employee_id = :e", u=fac, e=env["ids"]["reg"]) == 1
    assert _one(env, "SELECT count(*) FROM public.payments p JOIN training.orders o ON o.id = p.order_id") == 0
    assert _one(env, "SELECT count(*) FROM training.payments WHERE order_id = :o", o=order["id"]) == 1
    assert _one(env, "SELECT count(*) FROM public.audit_log WHERE actor_id = :e AND action <> 'login'", e=env["ids"]["reg"]) == 0
    assert _one(env, "SELECT count(*) FROM training.audit_log WHERE actor_id = :e AND action = 'order.create'", e=env["ids"]["reg"]) == 1
    # the facility's real cheque numbering was not used up
    assert _one(env, "SELECT count(*) FROM public.order_counters WHERE unit_id = :u", u=fac) == 0
    # nothing was queued for a real phone: a practice message is "sent" on the spot and never leaves the sandbox
    assert _one(env, "SELECT count(*) FROM public.outbox_messages WHERE unit_id = :u", u=fac) == 0
    assert _one(env, "SELECT count(*) FROM training.outbox_messages WHERE unit_id = :u AND status IN ('queued', 'sending')", u=fac) == 0
    # a colleague who works for real sees none of it
    assert c.get(f"{API}/orders/{order['id']}", headers=h_old).status_code == 404
    assert c.get(f"{API}/units/{fac}/orders", headers=h_old).json()["total"] == 0
    assert c.get(f"{API}/units/{fac}/orders", headers=h).json()["total"] == 1


# ----------------------------------------------------------------------------- what a trainee may not do


def test_everything_else_is_refused_while_in_training(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    fac, h = env["facility"], _h(env, "newadmin")
    st = _status(env, "newadmin")
    assert st["active"] is True and st["total"] == 7  # an administrator can do everything, so practises everything

    def refused(r: Any) -> bool:
        return r.status_code == 403 and r.json()["error"]["code"] == "training_mode"

    employees_before = _one(env, "SELECT count(*) FROM employees WHERE unit_id = :u", u=fac)
    assert refused(c.post(f"{API}/units/{fac}/employees", json={"fullName": "T-trn X", "login": f"t-trn-x-{RUN}", "roleId": env["roles"]["registrator"]}, headers=h))
    assert refused(c.put(f"{API}/employees/{env['ids']['old']}", json={"fullName": "T-trn changed"}, headers=h))
    assert refused(c.post(f"{API}/employees/{env['ids']['lab2']}/training/skip", headers=h))
    assert refused(c.post(f"{API}/units/{fac}/service-types", json={"name": "T-trn yangi", "categoryId": env["cat"], "price": 1}, headers=h))
    assert refused(c.delete(f"{API}/service-types/{env['st2']}", headers=h))
    assert refused(c.put(f"{API}/units/{fac}", json={"name": "T-trn renamed"}, headers=h))
    assert _one(env, "SELECT count(*) FROM employees WHERE unit_id = :u", u=fac) == employees_before
    assert _one(env, "SELECT full_name FROM employees WHERE id = :e", e=env["ids"]["old"]).startswith("T-trn old")
    # reading the real reference data is fine: the catalogue, colleagues, the tree
    assert c.get(f"{API}/units/{fac}/employees", headers=h).status_code == 200
    assert c.get(f"{API}/units/{fac}/service-types", headers=h).status_code == 200
    # the same requests from someone who works for real are not touched by the gate
    r = c.put(f"{API}/employees/{env['ids']['old']}", json={"phone": "+998 90 111-22-33"}, headers=_h(env, "mgr"))
    assert r.status_code == 200, r.text


def test_a_trainee_can_open_every_page_and_each_answer_says_where_they_are(env: dict[str, Any]) -> None:
    """Reading works on the sandbox connection (which may not write a real table) for every list the staff app
    opens, and every staff answer tells the page whether this employee is in the training."""
    c: TestClient = env["client"]
    fac, h = env["facility"], _h(env, "newadmin")
    period = {"dateFrom": "2026-01-01", "dateTo": "2026-12-31"}
    pages = ("reports/dashboard", "reports/breakdown", "reports/patients", "reports/results", "reports/services", "reports/results/list", "reports/sms", "orders", "orders/summary", "worklist", "worklist/counts", "documents", "outbox", "outbox/counts", "patients", "employees", "service-types", "categories", "roles")  # fmt: skip
    for page in pages:
        params = (period | {"by": "category"} if page == "reports/breakdown" else period) if page.startswith("reports/") else None
        r = c.get(f"{API}/units/{fac}/{page}", params=params, headers=h)
        assert r.status_code == 200, (page, r.text)
        assert r.headers["x-training"] == "1", page
    for path in ("/notifications", "/units/tree", "/auth/staff/me"):
        r = c.get(f"{API}{path}", headers=h)
        assert r.status_code == 200 and r.headers["x-training"] == "1", path
    # a refusal says it too; someone who works for real is told "0"; an anonymous answer says nothing
    r = c.post(f"{API}/units/{fac}/employees", json={}, headers=h)
    assert r.status_code == 403 and r.headers["x-training"] == "1"
    assert c.get(f"{API}/units/{fac}/orders", headers=_h(env, "mgr")).headers["x-training"] == "0"
    assert "x-training" not in c.get(f"{API}/health").headers
    assert c.get(f"{API}/health/ready").json()["training"] == "ready"


# ----------------------------------------------------------------------------- laboratory and doctor


def test_a_laboratory_trainee_gets_an_analysis_to_practise_on(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    fac, h = env["facility"], _h(env, "lab")
    st = _status(env, "lab")
    assert [t["key"] for t in st["tasks"]] == ["result_enter", "result_submit"] and all(t["available"] for t in st["tasks"])
    wl = c.get(f"{API}/units/{fac}/worklist", params={"status": ["pending", "entered"]}, headers=h).json()
    assert wl["items"], "something waits for a result in the sandbox"
    item = wl["items"][0]
    r = c.put(f"{API}/items/{item['id']}/values", json={"values": {"hb": 140, "rang": "tiniq"}}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "entered", r.text
    assert _tasks(_status(env, "lab")) == {"result_enter": True, "result_submit": False}
    r = c.post(f"{API}/items/{item['id']}/submit", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "submitted", r.text
    st = _status(env, "lab")
    assert st["done"] == 2 and st["canComplete"] is True
    assert _one(env, "SELECT count(*) FROM public.order_items WHERE id = :i", i=item["id"]) == 0
    env["submitted_item"] = item["id"]
    # asking again does not pile up samples
    before = _one(env, "SELECT count(*) FROM training.orders WHERE unit_id = :u", u=fac)
    _status(env, "lab")
    assert _one(env, "SELECT count(*) FROM training.orders WHERE unit_id = :u", u=fac) == before


def test_a_doctor_trainee_approves_and_the_paper_is_marked(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    fac, h = env["facility"], _h(env, "doc")
    st = _status(env, "doc")
    assert [t["key"] for t in st["tasks"]] == ["result_enter", "result_approve"] and all(t["available"] for t in st["tasks"])
    wl = c.get(f"{API}/units/{fac}/worklist", params={"status": ["submitted"]}, headers=h).json()
    assert wl["items"], "a result waits for approval in the sandbox"
    item = wl["items"][0]
    files_before = _one(env, "SELECT count(*) FROM stored_files")
    r = c.post(f"{API}/items/{item['id']}/approve", json={}, headers=h)
    assert r.status_code == 200, r.text
    doc = r.json()["document"]
    assert _tasks(_status(env, "doc")) == {"result_enter": False, "result_approve": True}
    pdf = c.get(f"{API}/documents/{doc['id']}/pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # the same template without the training stamp is a smaller file: the stamp is really in the page
    from app.modules.templates.renderer import render

    assert len(pdf.content) > len(render({"paper": "A4", "elements": []}, {}, None))
    # no real file, no real document, and the paper's public link leads nowhere
    assert _one(env, "SELECT count(*) FROM stored_files") == files_before
    assert _one(env, "SELECT count(*) FROM public.result_documents WHERE id = :d", d=doc["id"]) == 0
    token = _one(env, "SELECT public_token FROM training.result_documents WHERE id = :d", d=doc["id"])
    assert c.get(f"{API}/d/{token}").status_code == 404
    assert _one(env, "SELECT count(*) FROM public.outbox_messages WHERE unit_id = :u", u=fac) == 0
    # the other half of the doctor's training: entering a result
    wl = c.get(f"{API}/units/{fac}/worklist", params={"status": ["pending", "entered"]}, headers=h).json()
    assert wl["items"]
    r = c.put(f"{API}/items/{wl['items'][0]['id']}/values", json={"values": {"hb": 131, "rang": "tiniq"}}, headers=h)
    assert r.status_code == 200, r.text
    assert _status(env, "doc")["canComplete"] is True


# ----------------------------------------------------------------------------- finishing


def test_finishing_needs_every_task_and_then_the_data_is_real(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    fac = env["facility"]
    # not yet: a laboratory worker who has done nothing
    r = c.post(f"{API}/training/complete", params={"unitId": fac}, headers=_h(env, "lab2"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "training_incomplete"
    assert _one(env, "SELECT status FROM employee_training WHERE employee_id = :e", e=env["ids"]["lab2"]) == "active"

    h = _h(env, "reg")
    r = c.post(f"{API}/training/complete", params={"unitId": fac}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "passed" and r.json()["active"] is False and r.json()["completedAt"]
    assert r.headers["x-training"] == "0"  # the answer itself tells the page the training is over
    assert _one(env, "SELECT status FROM employee_training WHERE employee_id = :e", e=env["ids"]["reg"]) == "passed"
    assert _one(env, "SELECT count(*) FROM public.audit_log WHERE actor_id = :e AND action = 'training.complete'", e=env["ids"]["reg"]) == 1
    # what they practised on is gone
    assert _one(env, "SELECT count(*) FROM training.orders WHERE created_by_employee_id = :e", e=env["ids"]["reg"]) == 0
    assert _one(env, "SELECT count(*) FROM training.patients WHERE created_by = :e", e=env["ids"]["reg"]) == 0
    assert _one(env, "SELECT count(*) FROM training.audit_log WHERE actor_id = :e", e=env["ids"]["reg"]) == 0
    # from the very next request: the real data, with the same token
    assert c.get(f"{API}/auth/staff/me", headers=h).json()["training"] is False
    assert _status(env, "reg")["state"] == "passed"
    assert [p["id"] for p in c.get(f"{API}/units/{fac}/patients", headers=h).json()["items"]] == [env["real_patient"]]
    name = f"T-trn Birinchi Haqiqiy {RUN}"
    r = c.post(f"{API}/units/{fac}/patients", json={"fullName": name, "phone": phone("99894")}, headers=h)
    assert r.status_code == 201, r.text
    assert _one(env, "SELECT count(*) FROM public.patients WHERE full_name = :n", n=name) == 1
    assert _one(env, "SELECT count(*) FROM training.patients WHERE full_name = :n", n=name) == 0
    # finishing twice changes nothing
    assert c.post(f"{API}/training/complete", headers=h).json()["state"] == "passed"


def test_leftovers_of_people_who_no_longer_practise_are_swept(env: dict[str, Any]) -> None:
    """A clean-up that failed at the time (or a deleted account) must not leave practice rows for ever: the sweep at
    start removes what belongs to nobody who is still practising — and nothing of those who are."""
    reg, lab = env["ids"]["reg"], env["ids"]["lab"]  # reg has finished; lab is still in training

    async def _run(s) -> dict[str, int]:
        for who in (reg, lab):
            new_id = str(uuid.uuid4())
            await s.execute(text("CREATE TEMP TABLE _left ON COMMIT DROP AS SELECT * FROM public.patients WHERE id = :p"), {"p": env["real_patient"]})
            await s.execute(text("UPDATE _left SET id = :n, created_by = :w, full_name = 'T-trn Qoldiq', phone = :ph"), {"n": new_id, "w": who, "ph": phone("99895")})
            await s.execute(text("INSERT INTO training.patients SELECT * FROM _left"))
            await s.execute(text("DROP TABLE _left"))
        await s.commit()
        async with session_scope(training=True) as t:
            cleared = await samples.sweep(t)
        count = "SELECT count(*) FROM training.patients WHERE created_by = :w AND full_name = 'T-trn Qoldiq'"
        return {"cleared": cleared, "reg": (await s.execute(text(count), {"w": reg})).scalar_one(), "lab": (await s.execute(text(count), {"w": lab})).scalar_one()}

    seen = db(env["client"], _run)
    assert seen["cleared"] >= 1 and seen["reg"] == 0 and seen["lab"] == 1
    assert _one(env, "SELECT count(*) FROM public.patients WHERE id = :p AND deleted_at IS NULL", p=env["real_patient"]) == 1


# ----------------------------------------------------------------------------- a manager's side


def test_a_manager_lets_through_or_sends_back(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    fac, h_mgr = env["facility"], _h(env, "mgr")
    ids = env["ids"]
    _status(env, "lab2")  # in training, with a sample of their own in the sandbox

    page = c.get(f"{API}/units/{fac}/employees", params={"pageSize": 100}, headers=h_mgr).json()
    by_id = {e["id"]: e["training"] for e in page["items"]}
    assert by_id[ids["reg"]]["state"] == "passed" and by_id[ids["old"]]["state"] == "exempt" and by_id[ids["ghost"]]["state"] == "pending"
    lab2 = by_id[ids["lab2"]]
    assert (lab2["state"], lab2["done"], lab2["total"], lab2["completedAt"], lab2["completedByName"]) == ("active", 0, 2, None, None) and lab2["startedAt"]
    assert [(t["key"], t["done"]) for t in lab2["tasks"]] == [("result_enter", False), ("result_submit", False)]

    # only someone who may edit employees, never oneself, never while in training oneself
    assert c.post(f"{API}/employees/{ids['lab2']}/training/skip", headers=_h(env, "old")).status_code == 403
    r = c.post(f"{API}/employees/{ids['mgr']}/training/reset", headers=h_mgr)
    assert r.status_code == 403 and r.json()["error"]["code"] == "self_edit"

    r = c.post(f"{API}/employees/{ids['lab2']}/training/skip", headers=h_mgr)
    assert r.status_code == 200, r.text
    assert r.json()["training"]["state"] == "skipped" and r.json()["training"]["completedByName"] == f"T-trn mgr {RUN}"
    assert _status(env, "lab2")["active"] is False  # the same token works with the real data at once
    assert _one(env, "SELECT count(*) FROM training.orders WHERE created_by_employee_id = :e", e=ids["lab2"]) == 0
    assert _one(env, "SELECT count(*) FROM public.audit_log WHERE action = 'training.skip' AND entity_id = :e", e=ids["lab2"]) == 1
    # skipping someone who is not in training is a no-op
    assert c.post(f"{API}/employees/{ids['old']}/training/skip", headers=h_mgr).json()["training"]["state"] == "exempt"

    # back to school: an experienced colleague too
    r = c.post(f"{API}/employees/{ids['old']}/training/reset", headers=h_mgr)
    sent = r.json()["training"]
    assert r.status_code == 200 and (sent["state"], sent["done"], sent["total"], sent["completedAt"], sent["completedByName"]) == ("active", 0, 4, None, None)
    assert len(sent["tasks"]) == 4 and not any(t["done"] for t in sent["tasks"])
    assert _status(env, "old")["active"] is True
    assert c.get(f"{API}/units/{fac}/orders", headers=_h(env, "old")).status_code == 200
    assert c.post(f"{API}/employees/{ids['old']}/training/skip", headers=h_mgr).json()["training"]["state"] == "skipped"
    # a role with nothing to practise cannot be sent there
    r = c.post(f"{API}/employees/{ids['boss']}/training/reset", headers=h_mgr)
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_tasks"


def test_a_changed_role_changes_the_tasks(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    h_mgr, doc_id = _h(env, "mgr"), env["ids"]["doc"]
    assert _tasks(_status(env, "doc")) == {"result_enter": True, "result_approve": True}
    # doctor → receptionist: the tasks follow the role (what was done and is still theirs would stay done)
    r = c.put(f"{API}/employees/{doc_id}", json={"roleId": env["roles"]["registrator"]}, headers=h_mgr)
    assert r.status_code == 200, r.text
    st = _status(env, "doc")
    assert st["active"] is True and _tasks(st) == dict.fromkeys(("patient_create", "order_create", "order_add_item", "payment_take"), False)
    # … → a role with nothing to practise: the training is over, the real data from now on
    r = c.put(f"{API}/employees/{doc_id}", json={"roleId": env["roles"]["rahbar"]}, headers=h_mgr)
    assert r.status_code == 200, r.text
    st = _status(env, "doc")
    assert st["state"] == "exempt" and st["active"] is False
    assert c.get(f"{API}/auth/staff/me", headers=_h(env, "doc")).json()["training"] is False


def test_the_switch_turns_the_training_off(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = env["client"]
    h = _h(env, "newadmin")
    assert c.get(f"{API}/auth/staff/me", headers=h).json()["training"] is True
    monkeypatch.setattr(settings, "training_enabled", False)
    assert c.get(f"{API}/auth/staff/me", headers=h).json()["training"] is False
    assert _status(env, "newadmin")["state"] == "exempt"
    assert db(c, lambda _s: training_svc.is_active(uuid.UUID(env["ids"]["newadmin"]))) is False
    # (with the switch off they would work with the real data — nothing is written here)
    assert [p["id"] for p in c.get(f"{API}/units/{env['facility']}/patients", headers=h).json()["items"] if p["id"] == env["real_patient"]] == [env["real_patient"]]
    monkeypatch.setattr(settings, "training_enabled", True)
    assert c.get(f"{API}/auth/staff/me", headers=h).json()["training"] is True
