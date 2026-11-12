"""The Eskiz client without a database: login + token cache, the automatic re-login when a token is rejected,
the mapping of answers and errors, and the sending door that picks the provider."""

from __future__ import annotations

import uuid
from urllib.parse import parse_qsl

import httpx
import pytest
from app.core.config import settings
from app.core.crypto import encrypt
from app.infrastructure.db.models import MessagingAccount
from app.infrastructure.redis import cache
from app.modules.messaging import eskiz, sms, xabarchi
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
        return httpx.Response(404, json={"message": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE)
    monkeypatch.setattr(eskiz, "_http", lambda: client)
    return calls, state


async def test_login_caches_the_token_per_account(api) -> None:
    calls, state = api
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
    calls, state = api
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
    calls, state = api
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


def test_an_eskiz_row_needs_its_login() -> None:
    row = MessagingAccount(id=uuid.uuid4(), unit_id=uuid.uuid4(), channel="sms", provider="eskiz", api_key_enc=encrypt("secret"), api_key_masked="••••", default_priority="bulk", is_active=True, sender="4546")
    assert account_to_sms(row) is None
    row.login = "ses@example.uz"
    acc = account_to_sms(row)
    assert acc is not None and (acc.provider, acc.login, acc.sender, acc.priority, acc.account_id) == ("eskiz", "ses@example.uz", "4546", "bulk", row.id)
    row.provider = "xabarchi"
    assert account_to_sms(row).provider == "xabarchi"  # type: ignore[union-attr]
