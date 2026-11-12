"""One door for sending an SMS: the account says which provider (Xabarchi or Eskiz) and the client does the rest.
The outbox dispatcher and the "check connection" button both come through here."""

from __future__ import annotations

from app.core.config import settings
from app.core.crypto import decrypt
from app.modules.messaging import eskiz, xabarchi
from app.modules.messaging.providers import ProviderResult, SmsAccount, SmsProviderError

PROVIDER_NAMES = {"xabarchi": "Xabarchi", "eskiz": "Eskiz"}


def callback_url() -> str | None:
    """Where Eskiz reports deliveries: the explicit setting, else the public API address (none on localhost)."""
    if settings.eskiz_callback_url.strip():
        return settings.eskiz_callback_url.strip()
    base = settings.public_api_url.rstrip("/")
    if not base or "localhost" in base or "127.0.0.1" in base:
        return None
    return f"{base}/api/v1/messaging/eskiz/callback"


async def send(account: SmsAccount, to: str, text: str) -> ProviderResult:
    """Send one message through the account's provider. Raises `SmsProviderError` (give up) or
    `SmsTransientError` (retry later)."""
    secret = decrypt(account.key_enc) or ""
    if account.provider == "eskiz":
        return await eskiz.send_with_account(account.account_id or account.unit_id, account.login or "", secret, to, text, account.sender, callback_url())
    if account.provider == "xabarchi":
        results = await xabarchi.send_sms(secret, [to], text, account.priority)
        return results[0] if results else ProviderResult(to=to, provider_id=None, status="queued", raw={})
    raise SmsProviderError(f"Noma’lum SMS provayder: {account.provider}", code="sms_not_configured")


def test_text(account: SmsAccount, default: str) -> str:
    """The text the "check connection" button sends: Eskiz accepts only its fixed test text while an account is
    in test status (and keeps accepting it afterwards), so that is what goes out through Eskiz."""
    return eskiz.TEST_TEXT if account.provider == "eskiz" else default
