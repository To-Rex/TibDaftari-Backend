"""Eskiz.uz as a unit's SMS provider (test database, rows prefixed T-): the account is saved after a verified
login, units below inherit it, the "check connection" button sends Eskiz's test text, the dispatcher sends through
Eskiz with the account the message was queued with, and the delivery report flips the outbox row."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.crypto import decrypt
from app.infrastructure.db.models import OutboxMessage
from app.modules.messaging import dispatcher, eskiz
from app.modules.messaging.eskiz import EskizAuthError
from app.modules.messaging.providers import ProviderResult
from app.modules.units import repository as units_repo
from fastapi.testclient import TestClient

from tests.helpers import API, app_instance, bearer, db, make_employee, make_tree, seed, staff_token


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        admin = await make_employee(s, a.region, "admin")
        return {"region": str(a.region.id), "district": str(a.district.id), "facility": str(a.facility.id), "tag": a.tag, "admin": admin.login}

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["h"] = bearer(staff_token(client, data["admin"]))
        yield data


def test_eskiz_account_is_saved_after_a_verified_login(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    h, region = ctx["h"], ctx["region"]
    checked: list[tuple[str, str]] = []

    async def fake_check(account_id: Any, email: str, password: str) -> int:
        checked.append((email, password))
        return 5000

    monkeypatch.setattr(eskiz, "check_account", fake_check)
    # the cabinet e-mail is needed, and the password the first time
    r = c.put(f"{API}/units/{region}/sms", json={"mode": "own", "provider": "eskiz", "password": "secret"}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_login_required"
    r = c.put(f"{API}/units/{region}/sms", json={"mode": "own", "provider": "eskiz", "login": "ses@example.uz"}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_api_key_required"
    r = c.put(f"{API}/units/{region}/sms", json={"mode": "own", "provider": "eskiz", "login": "ses@example.uz", "password": "secret", "defaultPriority": "urgent"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["mode"], body["provider"], body["login"], body["sender"], body["apiKeyMasked"], body["effectiveUnitId"]) == ("own", "eskiz", "ses@example.uz", eskiz.DEFAULT_SENDER, "••••••••", region)
    assert "secret" not in r.text and checked == [("ses@example.uz", "secret")]
    # units below inherit it — the unit card says so too
    fac = c.get(f"{API}/units/{ctx['facility']}/sms", headers=h).json()
    assert (fac["mode"], fac["provider"], fac["login"], fac["sender"], fac["effectiveUnitId"]) == ("inherited", "eskiz", "ses@example.uz", eskiz.DEFAULT_SENDER, region)
    assert c.get(f"{API}/units/{ctx['facility']}", headers=h).json()["sms"]["provider"] == "eskiz"
    # re-saving keeps the password; the sender name changes
    r = c.put(f"{API}/units/{region}/sms", json={"mode": "own", "provider": "eskiz", "sender": "TEMO"}, headers=h)
    assert r.status_code == 200 and r.json()["sender"] == "TEMO" and r.json()["login"] == "ses@example.uz", r.text
    assert checked[-1] == ("ses@example.uz", "secret")

    async def row(s):
        return await units_repo.own_account(s, uuid.UUID(region), "sms")

    acc = db(c, row)
    assert acc.provider == "eskiz" and decrypt(acc.api_key_enc) == "secret" and acc.sender == "TEMO" and acc.login == "ses@example.uz"
    ctx["account_id"] = str(acc.id)


def test_wrong_credentials_are_refused_and_nothing_changes(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    h, region = ctx["h"], ctx["region"]

    async def bad(account_id: Any, email: str, password: str) -> int:
        raise EskizAuthError("Eskiz login yoki parol rad etildi (Invalid credentials)")

    monkeypatch.setattr(eskiz, "check_account", bad)
    r = c.put(f"{API}/units/{region}/sms", json={"mode": "own", "provider": "eskiz", "login": "other@example.uz", "password": "nope"}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_auth_error" and "rad etildi" in r.json()["error"]["message"]
    assert c.get(f"{API}/units/{region}/sms", headers=h).json()["login"] == "ses@example.uz"
    # switching to Xabarchi needs a Xabarchi key — the Eskiz password is never reused as one
    r = c.put(f"{API}/units/{region}/sms", json={"mode": "own", "provider": "xabarchi"}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_api_key_required"
    assert c.get(f"{API}/units/{region}/sms", headers=h).json()["provider"] == "eskiz"


def test_check_connection_sends_the_eskiz_test_text(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    sent: dict[str, Any] = {}

    async def fake_login(email: str, password: str) -> str:
        assert (email, password) == ("ses@example.uz", "secret")
        return "tok-x"

    async def fake_send(token: str, to: str, text: str, sender: str | None = None, callback_url: str | None = None) -> ProviderResult:
        sent.update(token=token, to=to, text=text, sender=sender)
        return ProviderResult(to=to, provider_id="req-1", status="waiting", raw={})

    async def fake_balance(token: str) -> int:
        return 4321

    monkeypatch.setattr(eskiz, "login", fake_login)
    monkeypatch.setattr(eskiz, "send_sms", fake_send)
    monkeypatch.setattr(eskiz, "balance", fake_balance)
    r = c.post(f"{API}/units/{ctx['facility']}/sms/test", json={"to": "+998 90 123 45 67"}, headers=ctx["h"])
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "provider": "eskiz", "providerMessageId": "req-1", "to": "998901234567", "balance": 4321}
    assert sent == {"token": "tok-x", "to": "998901234567", "text": eskiz.TEST_TEXT, "sender": "TEMO"}


def test_dispatcher_sends_through_eskiz_and_the_report_flips_the_row(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    calls: list[tuple] = []

    async def fake_send_with_account(account_id: Any, email: str, password: str, to: str, text: str, sender: str | None, callback_url: str | None = None) -> ProviderResult:
        calls.append((str(account_id), email, password, to, text, sender))
        return ProviderResult(to=to, provider_id="req-dispatch-1", status="waiting", raw={})

    monkeypatch.setattr(eskiz, "send_with_account", fake_send_with_account)

    async def make_and_deliver(s):
        msg = OutboxMessage(unit_id=uuid.UUID(ctx["facility"]), account_id=uuid.UUID(ctx["account_id"]), channel="sms", kind="reminder", to="998901234567", text=f"T-{ctx['tag']} eslatma", status="sending", attempts=0)
        s.add(msg)
        await s.flush()
        status = await dispatcher.deliver_one(s, msg)
        return status, msg.id, msg.provider_message_id

    status, msg_id, pid = db(c, make_and_deliver)
    assert status == "sent" and pid == "req-dispatch-1"
    assert calls == [(ctx["account_id"], "ses@example.uz", "secret", "998901234567", f"T-{ctx['tag']} eslatma", "TEMO")]

    async def fetch(s):
        row = await s.get(OutboxMessage, msg_id)
        return row.status, row.error

    # the delivery report (no login): an in-between state changes nothing, a final one does — JSON or a form
    assert c.post(f"{API}/messaging/eskiz/callback", json={"request_id": "req-dispatch-1", "status": "ACCEPTED"}).status_code == 200
    assert db(c, fetch) == ("sent", None)
    r = c.post(f"{API}/messaging/eskiz/callback", data={"request_id": "req-dispatch-1", "message_id": "4385062", "status": "DELIVRD", "status_date": "2026-11-12 10:00:00"})
    assert r.status_code == 200 and db(c, fetch) == ("delivered", None)
    # a late failure never undoes a delivery; unknown ids are ignored
    assert c.post(f"{API}/messaging/eskiz/callback", json={"request_id": "req-dispatch-1", "status": "EXPIRED"}).status_code == 200
    assert db(c, fetch) == ("delivered", None)
    assert c.post(f"{API}/messaging/eskiz/callback", json={"request_id": "nope", "status": "DELIVRD"}).status_code == 200
