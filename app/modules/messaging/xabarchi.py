"""Xabarchi SMS provider client — the only public endpoint is
`POST {base}/api/v1/public/messages` authenticated with `X-API-Key` (scope sms.send).

Request  {"to": ["998901234567", ...], "text": "...", "priority": "urgent|transactional|bulk"}
Response 201 → [{"id": 123, "to": "998901234567", "status": "queued", ...}, ...]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import ExternalServiceError

log = logging.getLogger("xabarchi")

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=settings.xabarchi_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.xabarchi_timeout_seconds, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "TibDaftari/1.0"},
        )
    return _client


@dataclass(slots=True)
class ProviderResult:
    to: str
    provider_id: str | None
    status: str
    raw: dict[str, Any]


class XabarchiError(ExternalServiceError):
    """Non-retryable provider rejection (401/403/422)."""


class XabarchiTransientError(ExternalServiceError):
    """Retryable failure (network, 5xx, 429)."""


def _detail(resp: httpx.Response) -> str:
    """Xabarchi error envelope is flat `{"code", "message"}`; fall back to raw text."""
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("message") or data.get("detail") or data.get("code") or resp.status_code)[:200]
        return str(data)[:200]
    except ValueError:
        return (resp.text or str(resp.status_code))[:200]


async def send_sms(api_key: str, to: list[str], text: str, priority: str = "transactional") -> list[ProviderResult]:
    if not api_key:
        raise XabarchiError("Xabarchi API kaliti sozlanmagan", code="sms_not_configured")
    body = {"to": to, "text": text, "priority": priority if priority in ("urgent", "transactional", "bulk") else "transactional"}
    try:
        resp = await _http().post("/api/v1/public/messages", json=body, headers={"X-API-Key": api_key})
    except httpx.HTTPError as exc:
        raise XabarchiTransientError(f"Xabarchi bilan aloqa yo‘q: {exc.__class__.__name__}") from exc
    if resp.status_code in (401, 403):
        raise XabarchiError(f"Xabarchi API kaliti rad etildi ({_detail(resp)})", code="sms_auth_error")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise XabarchiTransientError(f"Xabarchi vaqtincha javob bermadi ({resp.status_code})")
    if resp.status_code >= 400:
        raise XabarchiError(f"Xabarchi so‘rovni rad etdi ({resp.status_code}): {_detail(resp)}", code="sms_rejected")
    try:
        data = resp.json()
    except ValueError as exc:
        raise XabarchiTransientError("Xabarchi javobi o‘qilmadi") from exc
    items = data if isinstance(data, list) else data.get("items") or data.get("messages") or [data]
    out: list[ProviderResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(ProviderResult(to=str(item.get("to") or ""), provider_id=str(item["id"]) if item.get("id") is not None else None, status=str(item.get("status") or "queued"), raw=item))
    return out


async def close() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None
