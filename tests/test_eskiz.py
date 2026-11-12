"""Eskiz.uz as a unit's SMS provider (test database, rows prefixed T-): the account is saved after a verified
login, units below inherit it, the "check connection" button sends Eskiz's test text, the dispatcher sends through
Eskiz with the account the message was queued with, and the delivery report flips the outbox row."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.crypto import decrypt
from app.infrastructure.db.models import OutboxMessage
from app.modules.messaging import dispatcher, eskiz, eskiz_sync
from app.modules.messaging.eskiz import EskizAuthError
from app.modules.messaging.providers import ProviderResult
from app.modules.units import repository as units_repo
from fastapi.testclient import TestClient

from tests.helpers import API, app_instance, bearer, db, make_employee, make_tree, seed, staff_token

SFX = uuid.uuid4().hex[:6]


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
        return ProviderResult(to=to, provider_id=f"req-dispatch-{SFX}", status="waiting", raw={})

    monkeypatch.setattr(eskiz, "send_with_account", fake_send_with_account)

    async def make_and_deliver(s):
        msg = OutboxMessage(unit_id=uuid.UUID(ctx["facility"]), account_id=uuid.UUID(ctx["account_id"]), channel="sms", kind="reminder", to="998901234567", text=f"T-{ctx['tag']} eslatma", status="sending", attempts=0)
        s.add(msg)
        await s.flush()
        status = await dispatcher.deliver_one(s, msg)
        return status, msg.id, msg.provider_message_id

    status, msg_id, pid = db(c, make_and_deliver)
    assert status == "sent" and pid == f"req-dispatch-{SFX}"
    assert calls == [(ctx["account_id"], "ses@example.uz", "secret", "998901234567", f"T-{ctx['tag']} eslatma", "TEMO")]

    async def fetch(s):
        row = await s.get(OutboxMessage, msg_id)
        return row.status, row.error

    # the delivery report (no login): an in-between state changes nothing, a final one does — JSON or a form
    assert c.post(f"{API}/messaging/eskiz/callback", json={"request_id": f"req-dispatch-{SFX}", "status": "ACCEPTED"}).status_code == 200
    assert db(c, fetch) == ("sent", None)
    r = c.post(f"{API}/messaging/eskiz/callback", data={"request_id": f"req-dispatch-{SFX}", "message_id": "4385062", "status": "DELIVRD", "status_date": "2026-11-12 10:00:00"})
    assert r.status_code == 200 and db(c, fetch) == ("delivered", None)
    # a late failure never undoes a delivery; unknown ids are ignored
    assert c.post(f"{API}/messaging/eskiz/callback", json={"request_id": f"req-dispatch-{SFX}", "status": "EXPIRED"}).status_code == 200
    assert db(c, fetch) == ("delivered", None)
    assert c.post(f"{API}/messaging/eskiz/callback", json={"request_id": "nope", "status": "DELIVRD"}).status_code == 200


def test_the_eskiz_panel_balance_nicks_prices_templates(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    h = ctx["h"]
    asked: list[str] = []

    async def fake_login(email: str, password: str) -> str:
        return "tok-panel"

    async def fake_me(token: str) -> dict:
        asked.append("me")
        return {"id": 7, "name": "Xorazm SES", "email": "ses@example.uz", "status": "active", "balance": 152400}

    async def fake_balance(token: str) -> int:
        return 152400

    async def fake_nicks(token: str) -> list[str]:
        return ["4546", "XORAZMSES"]

    async def fake_prices(token: str) -> list[dict]:
        return [{"operator": "Beeline", "price": 95.0, "countryCode": "UZ"}, {"operator": "Ucell", "price": 110.0, "countryCode": "UZ"}]

    async def fake_templates(token: str) -> list[dict]:
        return [{"id": "1", "text": "Natija tayyor: {link}", "status": "service"}]

    for name, fn in (("login", fake_login), ("me", fake_me), ("balance", fake_balance), ("nicks", fake_nicks), ("prices", fake_prices), ("templates", fake_templates)):
        monkeypatch.setattr(eskiz, name, fn)
    # the facility inherits the region's Eskiz account → the panel is about that account
    r = c.get(f"{API}/units/{ctx['facility']}/sms/eskiz", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["configured"] is True and d["accountUnitId"] == ctx["region"] and d["login"] == "ses@example.uz" and d["sender"] == "TEMO"
    assert d["balance"] == 152400 and d["lowBalance"] is False and d["nicks"] == ["4546", "XORAZMSES"]
    assert [p["operator"] for p in d["prices"]] == ["Beeline", "Ucell"] and d["templates"][0]["status"] == "service" and d["userName"] == "Xorazm SES"
    assert d["fetchedAt"] and asked == ["me"]
    # cached for a minute: a second read asks nothing; refresh asks again
    assert c.get(f"{API}/units/{ctx['region']}/sms/eskiz", headers=h).json()["balance"] == 152400 and asked == ["me"]
    assert c.get(f"{API}/units/{ctx['region']}/sms/eskiz?refresh=true", headers=h).status_code == 200 and asked == ["me", "me"]
    # what a text costs: parts the operators bill, the price range over the operators, the template's state
    r = c.post(f"{API}/units/{ctx['facility']}/sms/check", json={"text": "Natija tayyor: {link}"}, headers=h)
    assert r.status_code == 200, r.text
    # the braces of a placeholder are GSM extension characters — two each
    assert r.json() == {"provider": "eskiz", "encoding": "gsm", "length": 23, "parts": 1, "priceFrom": 95.0, "priceTo": 110.0, "templateStatus": "service"}
    r = c.post(f"{API}/units/{ctx['facility']}/sms/check", json={"text": "Жавоб тайёр " * 10}, headers=h)
    assert r.json()["encoding"] == "ucs2" and r.json()["parts"] == 2 and r.json()["templateStatus"] is None
    # submitting a text for moderation
    submitted: list[str] = []

    async def fake_create(token: str, text: str) -> dict:
        submitted.append(text)
        return {"template": text, "status": "moderation", "id": 9}

    monkeypatch.setattr(eskiz, "create_template", fake_create)
    r = c.post(f"{API}/units/{ctx['facility']}/sms/eskiz/templates", json={"text": "Eslatma: {service}  "}, headers=h)
    assert r.status_code == 201 and submitted == ["Eslatma: {service}"] and asked == ["me", "me", "me"]
    # a unit whose SMS goes another way has no panel
    assert c.put(f"{API}/units/{ctx['region']}/sms", json={"mode": "off"}, headers=h).status_code == 200
    assert c.get(f"{API}/units/{ctx['facility']}/sms/eskiz", headers=h).json() == {"configured": False, "accountUnitId": None, "accountUnitName": None, "login": None, "sender": None, "balance": None, "lowBalance": False, "nicks": [], "prices": [], "templates": [], "userName": None, "userStatus": None, "fetchedAt": None, "error": None}
    assert c.post(f"{API}/units/{ctx['facility']}/sms/eskiz/templates", json={"text": "Salom"}, headers=h).status_code == 422

    async def fake_check(account_id: Any, email: str, password: str) -> int:
        return 1000

    monkeypatch.setattr(eskiz, "check_account", fake_check)
    assert c.put(f"{API}/units/{ctx['region']}/sms", json={"mode": "own", "provider": "eskiz", "login": "ses@example.uz", "password": "secret", "sender": "TEMO"}, headers=h).status_code == 200


def test_numbers_abroad_go_through_the_global_route(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    h = ctx["h"]
    # the home country keeps the 998 rule; another country takes full international numbers
    r = c.post(f"{API}/units/{ctx['facility']}/messages/send", json={"to": ["+7 701 123 45 67"], "text": "Salom", "kind": "reminder"}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_phone"
    r = c.post(f"{API}/units/{ctx['facility']}/messages/send", json={"to": ["+7 701 123 45 67", "12345"], "text": "Salom", "kind": "reminder", "countryCode": "kz"}, headers=h)
    assert r.status_code == 201, r.text
    row = r.json()[0]
    assert row["to"] == "77011234567" and row["countryCode"] == "KZ" and r.headers["X-Invalid-Recipients"] == "12345"
    sent: dict[str, Any] = {}

    async def fake_login(email: str, password: str) -> str:
        return "tok-g"

    async def fake_global(token: str, to: str, text: str, country_code: str, callback_url: str | None = None) -> ProviderResult:
        sent.update(to=to, text=text, country=country_code)
        return ProviderResult(to=to, provider_id="global-1", status="waiting", raw={})

    monkeypatch.setattr(eskiz, "login", fake_login)
    monkeypatch.setattr(eskiz, "send_global", fake_global)

    async def deliver(s):
        msg = await s.get(OutboxMessage, uuid.UUID(row["id"]))
        msg.status = "sending"
        status = await dispatcher.deliver_one(s, msg)
        return status, msg.provider_message_id

    assert db(c, deliver) == ("sent", "global-1") and sent == {"to": "77011234567", "text": "Salom", "country": "KZ"}


def test_a_batch_goes_out_in_one_request_and_the_sync_reads_its_states(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    batch_calls: list[dict] = []

    async def fake_login(email: str, password: str) -> str:
        return "tok-b"

    async def fake_batch(token: str, messages: Any, sender: str | None, dispatch_id: int, callback_url: str | None = None) -> dict:
        items = list(messages)
        batch_calls.append({"n": len(items), "sender": sender, "dispatch_id": dispatch_id, "keys": [m["user_sms_id"] for m in items]})
        return {"id": "batch-1", "status": ["waiting"] * len(items)}

    monkeypatch.setattr(eskiz, "login", fake_login)
    monkeypatch.setattr(eskiz, "send_batch", fake_batch)

    async def make(s):
        rows = [OutboxMessage(unit_id=uuid.UUID(ctx["facility"]), account_id=uuid.UUID(ctx["account_id"]), channel="sms", kind="broadcast", to=f"99890111223{i}", text=f"T-{ctx['tag']} paket {i}", status="sending", attempts=0) for i in range(3)]
        s.add_all(rows)
        await s.flush()
        accounts = {uuid.UUID(ctx["account_id"]): await s.get(type((await s.execute(__import__("sqlalchemy").select(__import__("app.infrastructure.db.models", fromlist=["MessagingAccount"]).MessagingAccount))).scalars().first()), uuid.UUID(ctx["account_id"]))}
        batches, singles = dispatcher._group_for_batch(rows, accounts)
        assert len(batches) == 1 and singles == []
        await dispatcher._deliver_eskiz_batch(s, batches[0][1], batches[0][0], datetime.now(UTC), accounts)
        for m in rows:
            m.sent_at = datetime.now(UTC)
        await s.flush()
        return [(m.id, m.status, m.provider_message_id, m.payload["eskizKey"], m.payload["dispatchId"]) for m in rows]

    out = db(c, make)
    assert batch_calls and batch_calls[0]["n"] == 3 and batch_calls[0]["sender"] == "TEMO"
    assert all(st == "sent" and pid == "batch-1" and len(key) == 12 and did == batch_calls[0]["dispatch_id"] for _, st, pid, key, did in out)
    keys = [key for _, _, _, key, _ in out]
    # the sync reads the dispatch list and flips the rows; the callback with user_sms_id flips a batch row too
    async def fake_by_dispatch(token: str, dispatch_id: int) -> list[dict]:
        return [{"user_sms_id": keys[0], "status": "DELIVERED", "parts_count": 1, "total_price": 95}, {"user_sms_id": keys[1], "status": "REJECTED"}]

    async def fake_status(token: str, request_id: str) -> dict | None:
        return None  # the single sends of the earlier tests: Eskiz does not know them (nothing changes)

    monkeypatch.setattr(eskiz, "messages_by_dispatch", fake_by_dispatch)
    monkeypatch.setattr(eskiz, "status_by_id", fake_status)

    async def sync(s):
        counts = await eskiz_sync.sync_eskiz_statuses(s, datetime.now(UTC))
        rows = [await s.get(OutboxMessage, mid) for mid, *_ in out]
        return counts, [(m.status, m.error, (m.payload.get("eskiz") or {}).get("totalPrice")) for m in rows]

    counts, states = db(c, sync)
    assert counts["delivered"] == 1 and counts["failed"] == 1 and counts["checked"] >= 3
    assert states[0] == ("delivered", None, 95.0) and states[1] == ("failed", "eskiz:REJECTED", None) and states[2][0] == "sent"
    r = c.post(f"{API}/messaging/eskiz/callback", json={"request_id": "batch-1", "user_sms_id": keys[2], "status": "DELIVRD", "sms_count": "1"})
    assert r.status_code == 200

    async def third(s):
        m = await s.get(OutboxMessage, out[2][0])
        return m.status, m.payload["eskiz"]["parts"]

    assert db(c, third) == ("delivered", 1)


def test_the_sms_report_counts_by_territory_day_kind_and_provider(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    h = ctx["h"]
    today = datetime.now(UTC).date().isoformat()
    r = c.get(f"{API}/units/{ctx['region']}/reports/sms?dateFrom={today}&dateTo={today}", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    # the rows the tests above queued at the facility: 3 batch + 1 abroad + 1 single dispatch of the earlier test
    assert d["totals"]["total"] >= 5 and d["totals"]["delivered"] >= 2 and d["totals"]["failed"] >= 1 and d["totals"]["cost"] >= 95
    unit = next(u for u in d["byUnit"] if u["unitId"] == ctx["facility"])
    assert unit["level"] == "facility" and unit["regionName"] == f"T-{ctx['tag']} viloyati" and unit["districtName"] == f"T-{ctx['tag']} tumani" and unit["total"] == d["totals"]["total"]
    assert len(d["byDay"]) == 1 and d["byDay"][0]["date"] == today and d["byDay"][0]["total"] == d["totals"]["total"]
    assert {k["key"] for k in d["byKind"]} >= {"broadcast", "reminder"} and d["byProvider"][0]["key"] == "eskiz"
    # narrowing to one facility and the Excel workbook
    assert c.get(f"{API}/units/{ctx['region']}/reports/sms?dateFrom={today}&dateTo={today}&unitId={ctx['facility']}", headers=h).json()["totals"] == d["totals"]
    x = c.get(f"{API}/units/{ctx['region']}/reports/sms/export.xlsx?dateFrom={today}&dateTo={today}&lang=uz", headers=h)
    assert x.status_code == 200 and x.content[:2] == b"PK" and "sms-hisobot" in x.headers["content-disposition"]
    # the cache key is per scope / range: a different day is empty
    r = c.get(f"{API}/units/{ctx['region']}/reports/sms?dateFrom=2020-01-01&dateTo=2020-01-02", headers=h)
    assert r.json()["totals"]["total"] == 0 and len(r.json()["byDay"]) == 2 and r.json()["byUnit"] == []
