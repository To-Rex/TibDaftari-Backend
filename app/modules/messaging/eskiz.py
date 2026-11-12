"""Eskiz.uz SMS client — `https://notify.eskiz.uz/api` (docs: documenter.getpostman.com/view/663428/RzfmES4z).

    POST  /auth/login                     form {email, password}            → {"data": {"token": "…"}}  (lives 30 days)
    GET   /auth/user                      Bearer                             → {"data": {"name", "email", "balance", …}}
    POST  /message/sms/send               Bearer, form {mobile_phone, message, from, callback_url?}
                                                                             → {"id": "<request uuid>", "status": "waiting"}
    GET   /message/sms/status_by_id/{id}  Bearer                             → {"data": {"status": "DELIVERED" | …}}
    GET   /user/get-limit                 Bearer                             → {"data": {"balance": N}}

A live account sends only texts that match a template approved in the Eskiz cabinet; an account in test status
sends only the fixed test texts (`TEST_TEXT`). Delivery reports arrive at `callback_url` as
{request_id, message_id, phone_number, status: DELIVRD | UNDELIV | EXPIRED | REJECTD | …, status_date}.
The bearer token is cached per account (`eskiz:token:{account_id}`) and renewed with a fresh login when Eskiz
answers 401.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.config import settings
from app.infrastructure.redis import cache
from app.modules.messaging.providers import ProviderResult, SmsProviderError, SmsTransientError

log = logging.getLogger("eskiz")

#: the sender name every Eskiz account starts with (replaced by the organisation's own nick once Eskiz approves it)
DEFAULT_SENDER = "4546"
#: one of the three texts an account in test status may send — and the one the "check connection" button uses
TEST_TEXT = "Bu Eskiz dan test"
TOKEN_TTL = 29 * 24 * 3600
TOKEN_KEY = "eskiz:token:{account_id}"
#: delivery-report states (overall and per part) that end a message's life
DELIVERED_STATES = frozenset({"DELIVRD", "DELIVERED", "PARTDELIVERED"})
FAILED_STATES = frozenset({"UNDELIV", "UNDELIVERABLE", "EXPIRED", "REJECTD", "REJECTED", "DELETED"})

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=settings.eskiz_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.eskiz_timeout_seconds, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "TibDaftari/1.0"},
        )
    return _client


class EskizError(SmsProviderError):
    """Non-retryable rejection (a rejected text / number, an unknown error)."""


class EskizAuthError(EskizError):
    """Wrong e-mail / password, or a token Eskiz no longer accepts."""

    code = "sms_auth_error"


class EskizTransientError(SmsTransientError):
    """Retryable failure (network, 5xx, 429)."""


def _detail(resp: httpx.Response) -> str:
    """Eskiz answers `{"message": …}` or `{"status": …, "data": {…}}`; fall back to the raw text."""
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or str(resp.status_code))[:200]
    if isinstance(data, dict):
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        return str(data.get("message") or inner.get("message") or data.get("error") or data.get("status") or resp.status_code)[:200]
    return str(data)[:200]


async def _request(method: str, path: str, *, token: str | None = None, data: dict[str, Any] | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = await _http().request(method, path, data=data, headers=headers)
    except httpx.HTTPError as exc:
        raise EskizTransientError(f"Eskiz bilan aloqa yo‘q: {exc.__class__.__name__}") from exc
    if resp.status_code == 429 or resp.status_code >= 500:
        raise EskizTransientError(f"Eskiz vaqtincha javob bermadi ({resp.status_code})")
    return resp


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        raise EskizTransientError("Eskiz javobi o‘qilmadi") from exc


async def login(email: str, password: str) -> str:
    """A fresh bearer token for the cabinet e-mail + password (wrong credentials → `EskizAuthError`)."""
    if not email or not password:
        raise EskizAuthError("Eskiz login yoki parol kiritilmagan", code="sms_not_configured")
    resp = await _request("POST", "/auth/login", data={"email": email, "password": password})
    if resp.status_code in (400, 401, 403, 422):
        raise EskizAuthError(f"Eskiz login yoki parol rad etildi ({_detail(resp)})")
    if resp.status_code >= 400:
        raise EskizError(f"Eskiz so‘rovni rad etdi ({resp.status_code}): {_detail(resp)}", code="sms_rejected")
    data = _json(resp)
    token = (data.get("data") or {}).get("token") if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
    if not token:
        raise EskizTransientError("Eskiz token qaytarmadi")
    return str(token)


async def token_for(account_id: uuid.UUID | str, email: str, password: str, *, force: bool = False) -> str:
    """The account's cached token, or a new one from a login (cached for 29 of its 30 days)."""
    key = TOKEN_KEY.format(account_id=account_id)
    if not force:
        cached = await cache.get_json(key)
        if isinstance(cached, str) and cached:
            return cached
    token = await login(email, password)
    await cache.set_json(key, token, TOKEN_TTL)
    return token


async def forget_token(account_id: uuid.UUID | str) -> None:
    await cache.delete(TOKEN_KEY.format(account_id=account_id))


async def send_sms(token: str, to: str, text: str, sender: str | None = None, callback_url: str | None = None) -> ProviderResult:
    """One message; `to` = digits with the country code (998…); the answer's `id` is the request id the delivery
    report quotes back."""
    form: dict[str, Any] = {"mobile_phone": to, "message": text, "from": (sender or DEFAULT_SENDER).strip()}
    if callback_url:
        form["callback_url"] = callback_url
    resp = await _request("POST", "/message/sms/send", token=token, data=form)
    if resp.status_code == 401:
        raise EskizAuthError("Eskiz token rad etildi")
    if resp.status_code >= 400:
        raise EskizError(f"Eskiz xabarni rad etdi ({resp.status_code}): {_detail(resp)}", code="sms_rejected")
    data = _json(resp)
    if not isinstance(data, dict):
        raise EskizTransientError("Eskiz javobi o‘qilmadi")
    rid = data.get("id")
    status = data.get("status")
    return ProviderResult(to=to, provider_id=str(rid) if rid else None, status=status if isinstance(status, str) and status else "waiting", raw=data)


async def balance(token: str) -> int | None:
    """The account's balance in so‘m (None when Eskiz does not say)."""
    resp = await _request("GET", "/user/get-limit", token=token)
    if resp.status_code == 401:
        raise EskizAuthError("Eskiz token rad etildi")
    if resp.status_code >= 400:
        return None
    data = _json(resp)
    try:
        return int((data.get("data") or {}).get("balance"))
    except (AttributeError, TypeError, ValueError):
        return None


async def _with_token[T](account_id: uuid.UUID | str, email: str, password: str, call: Callable[[str], Awaitable[T]]) -> T:
    """Run `call(token)`; when Eskiz rejects the token, log in afresh once and run it again."""
    token = await token_for(account_id, email, password)
    try:
        return await call(token)
    except EskizAuthError:
        await forget_token(account_id)
        token = await token_for(account_id, email, password, force=True)
        return await call(token)


async def send_with_account(account_id: uuid.UUID | str, email: str, password: str, to: str, text: str, sender: str | None, callback_url: str | None = None) -> ProviderResult:
    return await _with_token(account_id, email, password, lambda tok: send_sms(tok, to, text, sender, callback_url))


async def account_balance(account_id: uuid.UUID | str, email: str, password: str) -> int | None:
    return await _with_token(account_id, email, password, balance)


async def check_account(account_id: uuid.UUID | str, email: str, password: str) -> int | None:
    """The save path: a fresh login (wrong credentials → `EskizAuthError`), the token cached, the balance returned."""
    await forget_token(account_id)
    token = await token_for(account_id, email, password, force=True)
    return await balance(token)


async def close() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None
