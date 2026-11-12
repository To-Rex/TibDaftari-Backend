"""The Eskiz client without a database: login + token cache, the automatic re-login when a token is rejected,
the mapping of answers and errors, and the sending door that picks the provider."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl

import httpx
import pytest
from app.core.config import settings
from app.core.crypto import encrypt
from app.infrastructure.db.models import MessagingAccount
from app.infrastructure.redis import cache
from app.modules.messaging import dispatcher, eskiz, eskiz_sync, sms, xabarchi
from app.modules.messaging.providers import (
    ProviderResult,
    SmsAccount,
    SmsProviderError,
    SmsTransientError,
    account_to_sms,
)

BASE = "https://notify.test/api"


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    """A fake Eskiz: password `secret` logs in (tokens tok-1, tok-2 …); only the newest token sends."""
    calls: list[tuple[str, str, dict[str, str]]] = []
    state = {"logins": 0, "down": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        form = _form(request) if request.content else {}
        calls.append((request.method, path, form))
        if state["down"]:
            raise httpx.ConnectError("boom", request=request)
        if path.endswith("/auth/login"):
            if form.get("password") != "secret":
                return httpx.Response(401, json={"message": "Invalid credentials"})
            state["logins"] += 1
            return httpx.Response(200, json={"message": "token_generated", "data": {"token": f"tok-{state['logins']}"}, "token_type": "bearer"})
        if request.headers.get("Authorization") != f"Bearer tok-{state['logins']}":
            return httpx.Response(401, json={"message": "Unauthenticated."})
        if path.endswith("/message/sms/send"):
            if form.get("message") == "REJECT":
                return httpx.Response(400, json={"status": "error", "message": "Template not approved"})
            if form.get("message") == "DOWN":
                return httpx.Response(503, text="down")
            return httpx.Response(200, json={"id": "0f6b1d2e-req", "status": "waiting", "message": "Waiting for SMS provider"})
        if path.endswith("/user/get-limit"):
            return httpx.Response(200, json={"status": "success", "data": {"balance": 12345}})
        if path.endswith("/auth/user"):
            return httpx.Response(200, json={"status": "success", "data": {"id": 77, "name": "Xorazm SES", "email": "ses@example.uz", "status": "active", "balance": 12345}, "id": None})
        if path.endswith("/nick/me"):
            return httpx.Response(200, json=["4546", "XORAZMSES"])
        if path.endswith("/user/prices"):
            return httpx.Response(200, json={"status": "success", "data": [{"country_code": "UZ", "prices": [{"name": "Beeline", "price": 95}, {"name": "Ucell", "price": 100}, {"name": "UMS", "price": "110"}]}]})
        if path.endswith("/user/templates") and request.method == "GET":
            return httpx.Response(200, json={"success": True, "result": [{"id": 1, "template": "Natija tayyor: %w", "original_text": "Natija tayyor: {link}", "status": "service"}, {"id": 2, "template": "Chek", "original_text": "", "status": "moderation"}]})
        if path.endswith("/user/template"):
            return httpx.Response(200, json={"template": form.get("template"), "status": "moderation", "id": 3})
        if path.endswith("/message/sms/check"):
            body = json.loads(request.content or b"{}")
            return httpx.Response(200, json={"status": "success", "data": {"length": len(body.get("message", "")), "parts": 2, "total_price": 190}})
        if "/message/sms/status_by_id/" in path:
            rid = path.rsplit("/", 1)[-1]
            if rid == "unknown":
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"status": "success", "data": {"id": 5, "request_id": rid, "status": "DELIVERED", "parts_count": 2, "price": 95, "total_price": 190, "smsc_id": 3, "parts": [{"dlr_state": "DELIVRD"}, {"dlr_state": "DELIVRD"}]}, "id": None})
        if path.endswith("/message/sms/send-batch"):
            body = json.loads(request.content or b"{}")
            calls.append(("BATCH", path, body))
            return httpx.Response(200, json={"id": "batch-req-1", "message": "Waiting for SMS provider", "status": ["waiting" for _ in body.get("messages", [])]})
        if path.endswith("/message/sms/send-global"):
            return httpx.Response(200, json={"id": "global-req-1", "status": "waiting"})
        if path.endswith("/get-user-messages-by-dispatch"):
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"user_sms_id": "k1", "status": "DELIVERED"}, {"user_sms_id": "k2", "status": "REJECTED"}]}})
        return httpx.Response(404, json={"message": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE)
    monkeypatch.setattr(eskiz, "_http", lambda: client)
    return calls, state


async def test_login_caches_the_token_per_account(api) -> None:
    _calls, state = api
    acc = uuid.uuid4()
    assert await eskiz.token_for(acc, "ses@example.uz", "secret") == "tok-1"
    assert await eskiz.token_for(acc, "ses@example.uz", "secret") == "tok-1" and state["logins"] == 1
    await eskiz.forget_token(acc)
    assert await eskiz.token_for(acc, "ses@example.uz", "secret") == "tok-2"
    with pytest.raises(SmsProviderError) as exc:
        await eskiz.login("ses@example.uz", "wrong")
    assert exc.value.code == "sms_auth_error" and "rad etildi" in exc.value.message


async def test_a_rejected_token_means_one_fresh_login_and_a_second_try(api) -> None:
    calls, state = api
    acc = uuid.uuid4()
    await cache.set_json(eskiz.TOKEN_KEY.format(account_id=acc), "tok-stale", 60)
    r = await eskiz.send_with_account(acc, "ses@example.uz", "secret", "998901234567", "Natija tayyor", None, "https://api.test/cb")
    assert r.provider_id == "0f6b1d2e-req" and r.status == "waiting" and r.to == "998901234567"
    sends = [c for c in calls if c[1].endswith("/message/sms/send")]
    assert len(sends) == 2 and state["logins"] == 1
    assert sends[-1][2] == {"mobile_phone": "998901234567", "message": "Natija tayyor", "from": eskiz.DEFAULT_SENDER, "callback_url": "https://api.test/cb"}
    # the fresh token is cached for the next message
    assert await cache.get_json(eskiz.TOKEN_KEY.format(account_id=acc)) == "tok-1"


async def test_errors_are_sorted_into_give_up_and_retry(api) -> None:
    _calls, state = api
    acc = uuid.uuid4()
    with pytest.raises(SmsProviderError) as rejected:
        await eskiz.send_with_account(acc, "ses@example.uz", "secret", "998901234567", "REJECT", "TEMO")
    assert rejected.value.code == "sms_rejected" and "Template not approved" in rejected.value.message
    with pytest.raises(SmsTransientError):
        await eskiz.send_with_account(acc, "ses@example.uz", "secret", "998901234567", "DOWN", "TEMO")
    state["down"] = True
    with pytest.raises(SmsTransientError) as net:
        await eskiz.send_with_account(acc, "ses@example.uz", "secret", "998901234567", "hi", "TEMO")
    assert "aloqa" in net.value.message
    state["down"] = False
    assert await eskiz.check_account(acc, "ses@example.uz", "secret") == 12345
    with pytest.raises(SmsProviderError):
        await eskiz.check_account(acc, "ses@example.uz", "wrong")


async def test_the_door_picks_the_provider(api, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, _state = api
    acc = uuid.uuid4()
    esk = SmsAccount(key_enc=encrypt("secret"), priority="transactional", unit_id=uuid.uuid4(), account_id=acc, provider="eskiz", login="ses@example.uz", sender="TEMO")
    monkeypatch.setattr(settings, "eskiz_callback_url", "")
    monkeypatch.setattr(settings, "public_api_url", "http://localhost:8000")
    assert sms.callback_url() is None
    r = await sms.send(esk, "998901234567", "Chek 12 000 so‘m")
    assert r.provider_id == "0f6b1d2e-req"
    assert calls[-1][2]["from"] == "TEMO" and "callback_url" not in calls[-1][2]
    monkeypatch.setattr(settings, "public_api_url", "https://api.temo.uz/")
    assert sms.callback_url() == "https://api.temo.uz/api/v1/messaging/eskiz/callback"
    monkeypatch.setattr(settings, "eskiz_callback_url", "https://cb.example/x")
    assert sms.callback_url() == "https://cb.example/x"
    assert sms.test_text(esk, "default") == eskiz.TEST_TEXT

    seen: dict[str, object] = {}

    async def fake_xabarchi(api_key: str, to: list[str], text: str, priority: str = "transactional") -> list[ProviderResult]:
        seen.update(key=api_key, to=to, text=text, priority=priority)
        return [ProviderResult(to=to[0], provider_id="77", status="queued", raw={})]

    monkeypatch.setattr(xabarchi, "send_sms", fake_xabarchi)
    xab = SmsAccount(key_enc=encrypt("xab_live_1"), priority="urgent", unit_id=uuid.uuid4(), account_id=uuid.uuid4())
    r = await sms.send(xab, "998901234567", "hello")
    assert r.provider_id == "77" and seen == {"key": "xab_live_1", "to": ["998901234567"], "text": "hello", "priority": "urgent"}
    assert sms.test_text(xab, "default") == "default"
    with pytest.raises(SmsProviderError):
        await sms.send(SmsAccount(key_enc=encrypt("x"), priority="bulk", unit_id=uuid.uuid4(), provider="none"), "998901234567", "x")


async def test_the_account_surface_is_read_tolerantly(api) -> None:
    _calls, _state = api
    tok = await eskiz.login("ses@example.uz", "secret")
    assert (await eskiz.me(tok))["name"] == "Xorazm SES"
    assert await eskiz.nicks(tok) == ["4546", "XORAZMSES"]
    assert await eskiz.prices(tok) == [{"operator": "Beeline", "price": 95.0, "countryCode": "UZ"}, {"operator": "Ucell", "price": 100.0, "countryCode": "UZ"}, {"operator": "UMS", "price": 110.0, "countryCode": "UZ"}]
    assert await eskiz.templates(tok) == [{"id": "1", "text": "Natija tayyor: {link}", "status": "service"}, {"id": "2", "text": "Chek", "status": "moderation"}]
    assert (await eskiz.create_template(tok, "Eslatma: {service}"))["status"] == "moderation"
    checked = await eskiz.check_message(tok, "Salom dunyo")
    assert checked and (checked["length"], checked["parts"], checked["price"]) == (11, 2, 190.0)
    st = await eskiz.status_by_id(tok, "req-1")
    assert st and st["status"] == "DELIVERED" and st["total_price"] == 190
    assert await eskiz.status_by_id(tok, "unknown") is None
    assert await eskiz.messages_by_dispatch(tok, 123456789) == [{"user_sms_id": "k1", "status": "DELIVERED"}, {"user_sms_id": "k2", "status": "REJECTED"}]


async def test_batch_and_global_sends(api) -> None:
    calls, _state = api
    acc = uuid.uuid4()
    esk = SmsAccount(key_enc=encrypt("secret"), priority="transactional", unit_id=uuid.uuid4(), account_id=acc, provider="eskiz", login="ses@example.uz", sender="TEMO")
    r = await sms.send_batch(esk, [{"user_sms_id": "k1", "to": "998901234567", "text": "A"}, {"user_sms_id": "k2", "to": "998901234568", "text": "B"}], 123456789)
    assert r["id"] == "batch-req-1" and r["status"] == ["waiting", "waiting"]
    body = next(c[2] for c in calls if c[0] == "BATCH")
    assert body["from"] == "TEMO" and body["dispatch_id"] == 123456789 and [m["user_sms_id"] for m in body["messages"]] == ["k1", "k2"]
    g = await sms.send(esk, "77011234567", "Привет", country_code="KZ")
    assert g.provider_id == "global-req-1"
    sent = calls[-1][2]
    assert sent == {"mobile_phone": "77011234567", "message": "Привет", "country_code": "KZ", "unicode": "1"}
    xab = SmsAccount(key_enc=encrypt("xab"), priority="urgent", unit_id=uuid.uuid4(), account_id=uuid.uuid4())
    with pytest.raises(SmsProviderError):
        await sms.send(xab, "77011234567", "hi", country_code="KZ")
    assert sms.is_abroad("kz") and not sms.is_abroad("UZ") and not sms.is_abroad(None)


def test_sync_applies_eskiz_states_to_rows() -> None:
    now = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    msg = SimpleNamespace(status="sent", error=None, payload={"x": 1}, updated_at=None)
    assert eskiz_sync.apply_status(msg, {"status": "ACCEPTED", "parts": [{"dlr_state": "ACCEPTED"}], "parts_count": 1, "price": 95}, now) is None
    assert msg.status == "sent" and msg.payload["eskiz"]["status"] == "ACCEPTED" and msg.payload["eskiz"]["parts"] == 1 and msg.payload["x"] == 1
    assert eskiz_sync.apply_status(msg, {"status": "DELIVERED", "parts_count": 2, "total_price": 190, "smsc_id": 3}, now) == "delivered"
    assert msg.status == "delivered" and msg.payload["eskiz"]["totalPrice"] == 190.0 and msg.payload["eskiz"]["smsc"] == "3"
    # delivered never goes back; a failed part fails the message
    assert eskiz_sync.apply_status(msg, {"status": "ACCEPTED", "parts": [{"dlr_state": "EXPIRED"}]}, now) is None and msg.status == "delivered"
    other = SimpleNamespace(status="sent", error=None, payload={}, updated_at=None)
    assert eskiz_sync.apply_status(other, {"status": "ACCEPTED", "parts": [{"dlr_state": "UNDELIV"}]}, now) == "failed"
    assert other.status == "failed" and other.error == "eskiz:UNDELIV"
    unknown = SimpleNamespace(status="sent", error=None, payload={}, updated_at=None)
    assert eskiz_sync.apply_status(unknown, None, now) is None and unknown.payload["eskiz"]["syncedAt"] == now.isoformat()


def test_the_dispatcher_groups_eskiz_messages_into_batches() -> None:
    esk_id, xab_id = uuid.uuid4(), uuid.uuid4()
    accounts = {
        esk_id: MessagingAccount(id=esk_id, unit_id=uuid.uuid4(), channel="sms", provider="eskiz", api_key_enc=encrypt("s"), login="a@b", sender="4546", is_active=True),
        xab_id: MessagingAccount(id=xab_id, unit_id=uuid.uuid4(), channel="sms", provider="xabarchi", api_key_enc=encrypt("k"), is_active=True),
    }
    mk = lambda acc, payload=None, channel="sms": SimpleNamespace(channel=channel, account_id=acc, payload=payload or {})  # noqa: E731
    rows = [mk(esk_id), mk(esk_id), mk(esk_id, {"countryCode": "KZ"}), mk(xab_id), mk(xab_id), mk(None), mk(esk_id, channel="telegram")]
    batches, singles = dispatcher._group_for_batch(rows, accounts)
    assert len(batches) == 1 and batches[0][0].provider == "eskiz" and batches[0][1] == rows[:2]
    assert singles == [rows[2], rows[3], rows[4], rows[5], rows[6]]
    # a lone Eskiz message stays single
    batches, singles = dispatcher._group_for_batch([rows[0]], accounts)
    assert batches == [] and singles == [rows[0]]


def test_an_eskiz_row_needs_its_login() -> None:
    row = MessagingAccount(id=uuid.uuid4(), unit_id=uuid.uuid4(), channel="sms", provider="eskiz", api_key_enc=encrypt("secret"), api_key_masked="••••", default_priority="bulk", is_active=True, sender="4546")
    assert account_to_sms(row) is None
    row.login = "ses@example.uz"
    acc = account_to_sms(row)
    assert acc is not None and (acc.provider, acc.login, acc.sender, acc.priority, acc.account_id) == ("eskiz", "ses@example.uz", "4546", "bulk", row.id)
    row.provider = "xabarchi"
    assert account_to_sms(row).provider == "xabarchi"  # type: ignore[union-attr]
