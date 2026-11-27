"""The outbox seen as a journal you can act on (test database, rows prefixed T-outbox-): one message with the
names behind its ids, the day filter, taking a waiting message out of the queue and sending a finished one again."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from app.core.crypto import encrypt
from app.core.timeutil import local_date
from app.infrastructure.db.models import MessagingAccount, OutboxMessage, Patient
from fastapi.testclient import TestClient
from sqlalchemy import update

from tests.helpers import (
    API,
    COUNTRY_ID,
    app_instance,
    bearer,
    db,
    make_employee,
    make_role,
    make_tree,
    phone,
    seed,
    staff_token,
)

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-outbox-pass-1"
TEXT = f"T-outbox matn {SFX}"


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        s.add(MessagingAccount(unit_id=a.region.id, channel="sms", provider="xabarchi", api_key_enc=encrypt("xab_test_key"), api_key_masked="xab_••••", default_priority="transactional"))
        reader = await make_role(s, a.region, "reader", "T-outbox Reader", ["reports.operations.read"])
        admin_a = await make_employee(s, a.region, "admin", login=f"t-outbox-admin-a-{SFX}", password=PASSWORD, full_name=f"T-outbox Admin {SFX}")
        await make_employee(s, a.facility, role=reader, login=f"t-outbox-reader-a-{SFX}", password=PASSWORD)
        await make_employee(s, b.region, "admin", login=f"t-outbox-admin-b-{SFX}", password=PASSWORD)
        patient = Patient(country_id=COUNTRY_ID, registered_unit_id=a.facility.id, full_name=f"T-outbox Bemor {SFX}", phone=phone("99894"), tags=[])
        s.add(patient)
        await s.flush()
        now = datetime.now(UTC)

        def msg(unit, to: str, status: str, **kw) -> OutboxMessage:
            fields = {"channel": "sms", "kind": "reminder", "text": TEXT, "attempts": 0, "payload": {}, **kw}
            return OutboxMessage(unit_id=unit.id, to=to, status=status, **fields)

        made = {
            "failed": msg(a.facility, patient.phone, "failed", patient_id=patient.id, created_by=admin_a.id, error="Abonent topilmadi", attempts=3),
            "scheduled": msg(a.facility, "998900000021", "scheduled", scheduled_at=now + timedelta(days=1), next_attempt_at=now + timedelta(days=1)),
            "sending": msg(a.facility, "998900000022", "sending", next_attempt_at=now, leased_until=now + timedelta(minutes=5)),
            "retrying": msg(a.facility, "998900000023", "queued", next_attempt_at=now + timedelta(minutes=2), attempts=1, error="Xabarchi vaqtincha javob bermadi (503)"),
            "otp": msg(a.facility, "998900000024", "failed", kind="otp", text="Kod: ****", error="x"),
            "telegram": msg(a.facility, "5550001", "failed", channel="telegram", error="x"),
            "abroad": msg(a.facility, "77011234567", "delivered", payload={"countryCode": "KZ"}, sent_at=now),
            "old": msg(a.facility, "998900000025", "delivered", sent_at=now - timedelta(days=10)),
            "other": msg(b.facility, "998900000026", "failed", error="x"),
        }
        s.add_all(made.values())
        await s.flush()
        # written ten days ago (the column default is "now")
        await s.execute(update(OutboxMessage).where(OutboxMessage.id == made["old"].id).values(created_at=now - timedelta(days=10)))
        return {
            "a": {k: str(getattr(a, k).id) for k in ("region", "facility")},
            "b": {k: str(getattr(b, k).id) for k in ("region", "facility")},
            "admin_name": admin_a.full_name,
            "patient": {"id": str(patient.id), "name": patient.full_name, "phone": patient.phone},
            "m": {k: str(v.id) for k, v in made.items()},
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, f"t-outbox-{who}-{SFX}", PASSWORD) for who in ("admin-a", "reader-a", "admin-b")}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


def _rows(ctx: dict, who: str = "admin-a", **params) -> dict[str, dict]:
    r = ctx["client"].get(f"{API}/units/{ctx['a']['facility']}/outbox", params={"pageSize": 100, **params}, headers=h(ctx, who))
    assert r.status_code == 200, r.text
    return {m["id"]: m for m in r.json()["items"]}


def test_the_journal_names_people_and_says_what_can_be_done(ctx: dict) -> None:
    m = ctx["m"]
    rows = _rows(ctx)
    failed = rows[m["failed"]]
    # the ids are named: who it was for, who wrote it
    assert failed["patientId"] == ctx["patient"]["id"] and failed["patientName"] == ctx["patient"]["name"]
    assert failed["sentByName"] == ctx["admin_name"] and failed["orderNumber"] is None
    assert rows[m["scheduled"]]["sentByName"] is None and rows[m["scheduled"]]["patientName"] is None
    # what the holder of messaging.send may do, by state
    assert (failed["canResend"], failed["canCancel"]) == (True, False)
    assert (rows[m["scheduled"]]["canResend"], rows[m["scheduled"]]["canCancel"]) == (False, True)
    assert (rows[m["retrying"]]["canResend"], rows[m["retrying"]]["canCancel"]) == (False, True)
    # the dispatcher has it: shown as waiting, but nothing can be done to it any more
    assert rows[m["sending"]]["status"] == "queued" and (rows[m["sending"]]["canResend"], rows[m["sending"]]["canCancel"]) == (False, False)
    assert (rows[m["old"]]["canResend"], rows[m["old"]]["canCancel"]) == (True, False)
    # a code is masked for good and never repeated; a Telegram message is not an SMS
    assert rows[m["otp"]]["secret"] is True and rows[m["otp"]]["canResend"] is False
    assert rows[m["telegram"]]["secret"] is False and rows[m["telegram"]]["canResend"] is False
    # only a message that already failed once says when it is tried again
    assert rows[m["retrying"]]["nextAttemptAt"] is not None and rows[m["scheduled"]]["nextAttemptAt"] is None and failed["nextAttemptAt"] is None
    assert failed["resendOf"] is None
    # someone who may only read the journal is offered nothing
    seen = _rows(ctx, "reader-a")
    assert seen[m["failed"]]["patientName"] == ctx["patient"]["name"]
    assert not any(row["canResend"] or row["canCancel"] for row in seen.values())


def test_one_message_and_the_day_filter(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    m, fac = ctx["m"], ctx["a"]["facility"]
    r = c.get(f"{API}/units/{fac}/outbox/{m['failed']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json() == _rows(ctx)[m["failed"]]
    # from the region above it too; the reader may open it as well
    assert c.get(f"{API}/units/{ctx['a']['region']}/outbox/{m['failed']}", headers=h(ctx, "admin-a")).status_code == 200
    assert c.get(f"{API}/units/{fac}/outbox/{m['failed']}", headers=h(ctx, "reader-a")).json()["canResend"] is False
    # another tree's message is not here; another tree's admin is not let in; an unknown id is a 404, a bad one a 422
    assert c.get(f"{API}/units/{fac}/outbox/{m['other']}", headers=h(ctx, "admin-a")).status_code == 404
    assert c.get(f"{API}/units/{fac}/outbox/{m['failed']}", headers=h(ctx, "admin-b")).status_code == 403
    assert c.get(f"{API}/units/{fac}/outbox/{uuid.uuid4()}", headers=h(ctx, "admin-a")).status_code == 404
    assert c.get(f"{API}/units/{fac}/outbox/nope", headers=h(ctx, "admin-a")).status_code == 422

    # days are calendar days in Tashkent, both ends included
    today = local_date(datetime.now(UTC))
    old_day = local_date(datetime.now(UTC) - timedelta(days=10))
    recent = _rows(ctx, dateFrom=str(today - timedelta(days=1)))
    assert m["failed"] in recent and m["old"] not in recent
    past = _rows(ctx, dateTo=str(today - timedelta(days=5)))
    assert set(past) == {m["old"]}
    assert set(_rows(ctx, dateFrom=str(old_day), dateTo=str(old_day))) == {m["old"]}
    # the tab counters follow the same days
    counts = c.get(f"{API}/units/{fac}/outbox/counts", params={"dateTo": str(today - timedelta(days=5))}, headers=h(ctx, "admin-a")).json()
    assert counts["all"] == 1 and counts["delivered"] == 1 and counts["failed"] == 0
    assert c.get(f"{API}/units/{fac}/outbox", params={"dateFrom": "2026-13-45"}, headers=h(ctx, "admin-a")).status_code == 422


def test_a_waiting_message_can_be_taken_out_of_the_queue(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    m, fac = ctx["m"], ctx["a"]["facility"]
    url = lambda key: f"{API}/units/{fac}/outbox/{m[key]}/cancel"  # noqa: E731
    # reading the journal is not enough
    assert c.post(url("scheduled"), headers=h(ctx, "reader-a")).status_code == 403
    assert c.post(f"{API}/units/{fac}/outbox/{m['other']}/cancel", headers=h(ctx, "admin-a")).status_code == 404
    r = c.post(url("scheduled"), headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "cancelled" and out["canCancel"] is False and out["canResend"] is True
    # a second time, or anything the dispatcher already has or finished: too late
    for key in ("scheduled", "sending", "failed", "old"):
        r = c.post(url(key), headers=h(ctx, "admin-a"))
        assert r.status_code == 409 and r.json()["error"]["code"] == "message_not_cancellable", (key, r.text)
    # a message waiting for another try can still be stopped — and it leaves the queue for good
    r = c.post(url("retrying"), headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["nextAttemptAt"] is None

    async def queue_state(s):
        row = await s.get(OutboxMessage, uuid.UUID(m["retrying"]))
        return row.status, row.next_attempt_at, row.leased_until

    assert db(c, queue_state) == ("cancelled", None, None)


def test_a_finished_message_can_be_sent_again(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    m, fac = ctx["m"], ctx["a"]["facility"]
    url = lambda key: f"{API}/units/{fac}/outbox/{m[key]}/resend"  # noqa: E731
    code = lambda r: r.json()["error"]["code"]  # noqa: E731
    assert c.post(url("failed"), headers=h(ctx, "reader-a")).status_code == 403
    assert c.post(f"{API}/units/{fac}/outbox/{m['other']}/resend", headers=h(ctx, "admin-a")).status_code == 404
    # a new message goes out; the original keeps its own outcome
    r = c.post(url("failed"), headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["id"] != m["failed"] and copy["resendOf"] == m["failed"] and copy["status"] == "queued" and copy["attempts"] == 0 and copy["error"] is None
    assert (copy["to"], copy["text"], copy["kind"], copy["unitId"]) == (ctx["patient"]["phone"], TEXT, "reminder", fac)
    assert copy["patientId"] == ctx["patient"]["id"] and copy["patientName"] == ctx["patient"]["name"] and copy["sentByName"] == ctx["admin_name"]
    assert (copy["canCancel"], copy["canResend"]) == (True, False)
    rows = _rows(ctx)
    assert rows[m["failed"]]["status"] == "failed" and rows[copy["id"]]["resendOf"] == m["failed"]
    # while that copy is on its way the original is not repeated a second time (a double click is one message)
    r = c.post(url("failed"), headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and code(r) == "message_already_resent"
    # … and the copy itself is still going out
    r = c.post(f"{API}/units/{fac}/outbox/{copy['id']}/resend", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and code(r) == "message_in_flight"
    # once the copy has failed too, another try is allowed
    assert c.post(f"{API}/units/{fac}/outbox/{copy['id']}/cancel", headers=h(ctx, "admin-a")).status_code == 200
    again = c.post(url("failed"), headers=h(ctx, "admin-a"))
    assert again.status_code == 201 and again.json()["id"] not in (copy["id"], m["failed"])
    # a delivered message can be repeated as well (the patient lost it) — a number abroad keeps its country
    r = c.post(url("abroad"), headers=h(ctx, "admin-a"))
    assert r.status_code == 201 and r.json()["countryCode"] == "KZ" and r.json()["to"] == "77011234567"
    # never: a code or a password, a Telegram message, something still in the queue or with the dispatcher
    for key, why in (("otp", "message_secret"), ("telegram", "message_not_resendable"), ("sending", "message_in_flight")):
        r = c.post(url(key), headers=h(ctx, "admin-a"))
        assert r.status_code == 409 and code(r) == why, (key, r.text)
