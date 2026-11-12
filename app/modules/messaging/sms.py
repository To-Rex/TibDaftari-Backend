"""One door for sending an SMS: the account says which provider (Xabarchi or Eskiz) and the client does the rest.
The outbox dispatcher and the "check connection" button both come through here; Eskiz accounts also get batches
(many messages, one request) and numbers outside Uzbekistan (`send-global`)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.core.config import settings
from app.core.crypto import decrypt
from app.modules.messaging import eskiz, xabarchi
from app.modules.messaging.providers import ProviderResult, SmsAccount, SmsProviderError

PROVIDER_NAMES = {"xabarchi": "Xabarchi", "eskiz": "Eskiz"}
HOME_COUNTRY = "UZ"


def callback_url() -> str | None:
    """Where Eskiz reports deliveries: the explicit setting, else the public API address (none on localhost)."""
    if settings.eskiz_callback_url.strip():
        return settings.eskiz_callback_url.strip()
    base = settings.public_api_url.rstrip("/")
    if not base or "localhost" in base or "127.0.0.1" in base:
        return None
    return f"{base}/api/v1/messaging/eskiz/callback"


def is_abroad(country_code: str | None) -> bool:
    return bool(country_code) and str(country_code).strip().upper() != HOME_COUNTRY


async def send(account: SmsAccount, to: str, text: str, *, country_code: str | None = None) -> ProviderResult:
    """Send one message through the account's provider. A number outside Uzbekistan (`country_code` ≠ UZ) goes out
    through Eskiz's global route; Xabarchi has none. Raises `SmsProviderError` (give up) or `SmsTransientError`
    (retry later)."""
    secret = decrypt(account.key_enc) or ""
    abroad = is_abroad(country_code)
    if account.provider == "eskiz":
        if abroad:
            return await eskiz.call_with_account(account.account_id or account.unit_id, account.login or "", secret, lambda tok: eskiz.send_global(tok, to, text, str(country_code), callback_url()))
        return await eskiz.send_with_account(account.account_id or account.unit_id, account.login or "", secret, to, text, account.sender, callback_url())
    if account.provider == "xabarchi":
        if abroad:
            raise SmsProviderError("Xabarchi xalqaro SMS yubormaydi — bu birlik uchun Eskiz hisobi kerak", code="sms_rejected")
        results = await xabarchi.send_sms(secret, [to], text, account.priority)
        return results[0] if results else ProviderResult(to=to, provider_id=None, status="queued", raw={})
    raise SmsProviderError(f"Noma’lum SMS provayder: {account.provider}", code="sms_not_configured")


async def send_batch(account: SmsAccount, messages: Iterable[dict[str, Any]], dispatch_id: int) -> dict[str, Any]:
    """Many messages in one Eskiz request (`messages` = [{user_sms_id, to, text}]) → `{id, status: [...]}`."""
    if account.provider != "eskiz":
        raise SmsProviderError("Paketli yuborish faqat Eskiz hisobi bilan", code="sms_not_configured")
    secret = decrypt(account.key_enc) or ""
    return await eskiz.call_with_account(account.account_id or account.unit_id, account.login or "", secret, lambda tok: eskiz.send_batch(tok, messages, account.sender, dispatch_id, callback_url()))


def test_text(account: SmsAccount, default: str) -> str:
    """The text the "check connection" button sends: Eskiz accepts only its fixed test text while an account is
    in test status (and keeps accepting it afterwards), so that is what goes out through Eskiz."""
    return eskiz.TEST_TEXT if account.provider == "eskiz" else default
