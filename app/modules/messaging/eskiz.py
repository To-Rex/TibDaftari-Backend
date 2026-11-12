"""Eskiz.uz SMS client — `https://notify.eskiz.uz/api` (docs: documenter.getpostman.com/view/663428/RzfmES4z).

    POST  /auth/login                     form {email, password}            → {"data": {"token": "…"}}  (lives 30 days)
    GET   /auth/user                      Bearer                             → {"data": {"name", "email", "balance", …}}
    POST  /message/sms/send               Bearer, form {mobile_phone, message, from, callback_url?}
                                                                             → {"id": "<request uuid>", "status": "waiting"}
    GET   /message/sms/status_by_id/{id}  Bearer                             → {"data": {"status": "DELIVERED" | …}}
    GET   /user/get-limit                 Bearer                             → {"data": {"balance": N}}
    GET   /nick/me · /user/prices · /user/templates · POST /user/template {template} · GET /message/sms/check {message}
    POST  /message/sms/send-batch         JSON {messages: [{user_sms_id, to, text}], from, dispatch_id, callback_url?}
    POST  /message/sms/send-global        form {mobile_phone, message, country_code, callback_url?, unicode}
    POST  /message/sms/get-user-messages-by-dispatch {dispatch_id} · POST /user/totals {year, month}

A live account sends only texts that match a template approved in the Eskiz cabinet; an account in test status
sends only the fixed test texts (`TEST_TEXT`). Delivery reports arrive at `callback_url` as
{request_id, message_id, phone_number, status: DELIVRD | UNDELIV | EXPIRED | REJECTD | …, status_date}.
The bearer token is cached per account (`eskiz:token:{account_id}`) and renewed with a fresh login when Eskiz
answers 401.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
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


async def _request(method: str, path: str, *, token: str | None = None, data: dict[str, Any] | None = None, json_body: Any = None, params: dict[str, Any] | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        if json_body is not None:
            # Eskiz takes a few bodies as raw JSON — on GET too (`/message/sms/check`)
            resp = await _http().request(method, path, content=json.dumps(json_body), headers={**headers, "Content-Type": "application/json"}, params=params)
        else:
            resp = await _http().request(method, path, data=data, headers=headers, params=params)
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


def _auth_or_error(resp: httpx.Response, what: str) -> None:
    """401 → the token is dead (re-login); any other 4xx → the request itself was refused."""
    if resp.status_code == 401:
        raise EskizAuthError("Eskiz token rad etildi")
    if resp.status_code >= 400:
        raise EskizError(f"Eskiz {what} ({resp.status_code}): {_detail(resp)}", code="sms_rejected")


def _payload(data: Any) -> Any:
    """The useful part of an Eskiz answer: `data` / `result` when the envelope has one, else the answer itself."""
    if isinstance(data, dict):
        for key in ("data", "result"):
            if key in data and data[key] is not None:
                return data[key]
    return data


def _first_list(data: Any) -> list[Any]:
    """The first list of items inside a (possibly nested) Eskiz answer."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for value in data.values():
            found = _first_list(value)
            if found:
                return found
    return []


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


# ----------------------------------------------------------------------------- the account


async def me(token: str) -> dict[str, Any]:
    """The cabinet account: `{id, name, email, status, is_vip, balance, …}`."""
    resp = await _request("GET", "/auth/user", token=token)
    _auth_or_error(resp, "hisobni bermadi")
    data = _payload(_json(resp))
    return data if isinstance(data, dict) else {}


async def nicks(token: str) -> list[str]:
    """The sender names (nicks) Eskiz approved for the account — `["4546", "TEMO", …]`."""
    resp = await _request("GET", "/nick/me", token=token)
    _auth_or_error(resp, "yuboruvchi nomlarini bermadi")
    out: list[str] = []
    for item in _first_list(_json(resp)):
        name = item if isinstance(item, str) else (item.get("nick") or item.get("name") or item.get("title")) if isinstance(item, dict) else None
        if name and str(name).strip() and str(name).strip() not in out:
            out.append(str(name).strip())
    return out


async def prices(token: str) -> list[dict[str, Any]]:
    """The price of one SMS part per operator: `[{operator, price, countryCode?}]` (the answer's shape varies —
    every dict carrying a name and a price is taken)."""
    resp = await _request("GET", "/user/prices", token=token)
    _auth_or_error(resp, "narxlarni bermadi")
    out: list[dict[str, Any]] = []

    def walk(node: Any, country: str | None) -> None:
        if isinstance(node, dict):
            cc = node.get("country_code") or node.get("country") or country
            name = node.get("name") or node.get("operator") or node.get("smsc") or node.get("title")
            price = _num(node.get("price") if node.get("price") is not None else node.get("cost"))
            if name and price is not None and not isinstance(node.get("prices"), list):
                out.append({"operator": str(name), "price": price, "countryCode": str(cc) if cc else None})
                return
            for value in node.values():
                walk(value, str(cc) if cc else country)
        elif isinstance(node, list):
            for item in node:
                walk(item, country)

    walk(_json(resp), None)
    # one row per operator and country (the cheapest when Eskiz lists it twice), the home country first, cheapest first
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in out:
        key = (str(row["operator"]).strip().casefold(), (row["countryCode"] or "").upper())
        if key not in best or row["price"] < best[key]["price"]:
            best[key] = {**row, "countryCode": row["countryCode"].upper() if row["countryCode"] else None}
    return sorted(best.values(), key=lambda r: ((r["countryCode"] or "UZ") != "UZ", r["countryCode"] or "", r["price"], r["operator"]))


async def templates(token: str) -> list[dict[str, Any]]:
    """The account's templates and their moderation state: `[{id, text, status}]`
    (status: moderation | inproccess | service | reklama | rejected)."""
    resp = await _request("GET", "/user/templates", token=token)
    _auth_or_error(resp, "shablonlarni bermadi")
    out = []
    for item in _first_list(_json(resp)):
        if isinstance(item, dict) and (item.get("template") or item.get("original_text")):
            out.append({"id": str(item.get("id") or ""), "text": str(item.get("original_text") or item.get("template") or ""), "status": str(item.get("status") or "moderation")})
    return out


async def create_template(token: str, text: str) -> dict[str, Any]:
    """Submit a text for moderation; Eskiz answers with the template (and sometimes its id / status)."""
    resp = await _request("POST", "/user/template", token=token, data={"template": text})
    _auth_or_error(resp, "shablonni qabul qilmadi")
    data = _payload(_json(resp))
    return data if isinstance(data, dict) else {"template": text}


async def check_message(token: str, text: str) -> dict[str, Any] | None:
    """What Eskiz says a text costs — `{length, parts, price?}`; None when the answer cannot be read."""
    try:
        resp = await _request("GET", "/message/sms/check", token=token, json_body={"message": text})
    except SmsTransientError:
        return None
    if resp.status_code == 401:
        raise EskizAuthError("Eskiz token rad etildi")
    if resp.status_code >= 400:
        return None
    try:
        data = _payload(resp.json())
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    parts = _int(data.get("parts") if not isinstance(data.get("parts"), list) else len(data["parts"])) or _int(data.get("parts_count")) or _int(data.get("sms_count"))
    length = _int(data.get("length")) or _int(data.get("characters")) or _int(data.get("symbols"))
    price = _num(data.get("total_price")) if data.get("total_price") is not None else _num(data.get("price"))
    return {"parts": parts, "length": length, "price": price, "raw": data}


async def status_by_id(token: str, request_id: str) -> dict[str, Any] | None:
    """The state of one sent message by its request id (UUID, last 30 days) — `{status, parts: [{dlr_state, …}],
    parts_count, price, total_price, smsc_id, …}`; None when Eskiz does not know it."""
    resp = await _request("GET", f"/message/sms/status_by_id/{request_id}", token=token)
    if resp.status_code == 401:
        raise EskizAuthError("Eskiz token rad etildi")
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise EskizError(f"Eskiz holatni bermadi ({resp.status_code}): {_detail(resp)}", code="sms_rejected")
    data = _payload(_json(resp))
    return data if isinstance(data, dict) and data else None


async def send_batch(token: str, messages: Iterable[dict[str, Any]], sender: str | None, dispatch_id: int, callback_url: str | None = None) -> dict[str, Any]:
    """Many messages in one request: `messages` = [{user_sms_id (≤ 12 chars), to, text}]; the answer carries ONE
    request id for the whole batch and a status per message."""
    body: dict[str, Any] = {"messages": list(messages), "from": (sender or DEFAULT_SENDER).strip(), "dispatch_id": dispatch_id}
    if callback_url:
        body["callback_url"] = callback_url
    resp = await _request("POST", "/message/sms/send-batch", token=token, json_body=body)
    _auth_or_error(resp, "paketni rad etdi")
    data = _json(resp)
    return data if isinstance(data, dict) else {"id": None, "status": data}


async def send_global(token: str, to: str, text: str, country_code: str, callback_url: str | None = None) -> ProviderResult:
    """One message to a number outside Uzbekistan (`country_code` = ISO-2, e.g. KZ); Cyrillic / non-GSM text goes
    as unicode."""
    form: dict[str, Any] = {"mobile_phone": to, "message": text, "country_code": country_code.upper(), "unicode": 0 if segments(text)[0] == "gsm" else 1}
    if callback_url:
        form["callback_url"] = callback_url
    resp = await _request("POST", "/message/sms/send-global", token=token, data=form)
    _auth_or_error(resp, "xalqaro xabarni rad etdi")
    data = _json(resp)
    if not isinstance(data, dict):
        data = {}
    rid = data.get("id")
    status = data.get("status")
    return ProviderResult(to=to, provider_id=str(rid) if rid else None, status=status if isinstance(status, str) and status else "waiting", raw=data)


async def messages_by_dispatch(token: str, dispatch_id: int) -> list[dict[str, Any]]:
    """The messages of one batch (dispatch) with their states — matched back by `user_sms_id`."""
    resp = await _request("POST", "/message/sms/get-user-messages-by-dispatch", token=token, data={"dispatch_id": dispatch_id, "count": 0, "is_ad": 0}, params={"page-size": 1000})
    if resp.status_code == 401:
        raise EskizAuthError("Eskiz token rad etildi")
    if resp.status_code >= 400:
        raise EskizError(f"Eskiz paket holatini bermadi ({resp.status_code}): {_detail(resp)}", code="sms_rejected")
    try:
        data = resp.json()
    except ValueError:
        return []
    return [item for item in _first_list(data) if isinstance(item, dict)]


async def totals(token: str, year: int, month: int) -> list[dict[str, Any]]:
    """Eskiz's own monthly totals (`[{status, month, packets}]` — the shape is theirs); [] when unreadable."""
    try:
        resp = await _request("POST", "/user/totals", token=token, data={"year": year, "month": month, "is_global": 0})
    except SmsTransientError:
        return []
    if resp.status_code == 401:
        raise EskizAuthError("Eskiz token rad etildi")
    if resp.status_code >= 400:
        return []
    try:
        return [item for item in _first_list(resp.json()) if isinstance(item, dict)]
    except ValueError:
        return []


# ----------------------------------------------------------------------------- what one text costs

GSM_BASIC = "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
GSM_EXT = "^{}\\[~]|€"


def segments(text: str) -> tuple[str, int, int]:
    """(encoding gsm | ucs2, length, parts) the way operators bill a text: GSM-7 fits 160 characters in one part and
    153 per part after that (extension characters count twice); anything else is UCS-2 — 70 / 67."""
    gsm = True
    length = 0
    for ch in text:
        if ch in GSM_BASIC:
            length += 1
        elif ch in GSM_EXT:
            length += 2
        else:
            gsm = False
            break
    if not gsm:
        length = len(text)
    single, multi = (160, 153) if gsm else (70, 67)
    parts = 0 if length == 0 else 1 if length <= single else -(-length // multi)
    return ("gsm" if gsm else "ucs2"), length, parts


async def _with_token[T](account_id: uuid.UUID | str, email: str, password: str, call: Callable[[str], Awaitable[T]]) -> T:
    """Run `call(token)`; when Eskiz rejects the token, log in afresh once and run it again."""
    token = await token_for(account_id, email, password)
    try:
        return await call(token)
    except EskizAuthError:
        await forget_token(account_id)
        token = await token_for(account_id, email, password, force=True)
        return await call(token)


async def call_with_account[T](account_id: uuid.UUID | str, email: str, password: str, call: Callable[[str], Awaitable[T]]) -> T:
    """Any client function with the account's token (`lambda tok: nicks(tok)`), re-logging in once if needed."""
    return await _with_token(account_id, email, password, call)


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
