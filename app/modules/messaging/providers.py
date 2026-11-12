"""What every SMS provider client shares — the account a message goes out through, the result of one send and the
two error kinds the outbox reacts to (give up vs. retry). The clients themselves: `xabarchi.py`, `eskiz.py`;
the door that picks one: `sms.py`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import ExternalServiceError
from app.infrastructure.db.models import MessagingAccount

#: the SMS providers a unit may enter an account for
SMS_PROVIDERS: tuple[str, ...] = ("xabarchi", "eskiz")


@dataclass(slots=True)
class ProviderResult:
    to: str
    provider_id: str | None
    status: str
    raw: dict[str, Any]


class SmsProviderError(ExternalServiceError):
    """Non-retryable rejection: wrong credentials, a rejected text / number, an unknown provider."""


class SmsTransientError(ExternalServiceError):
    """Retryable failure: network, 5xx, 429."""


@dataclass(frozen=True, slots=True)
class SmsAccount:
    """The SMS account a message goes out through (a Xabarchi key or an Eskiz login + password)."""

    key_enc: str
    priority: str
    #: the unit the account sits on (the facility itself or an ancestor whose account it inherits)
    unit_id: uuid.UUID
    #: the `messaging_accounts` row (recorded on the outbox row so the dispatcher sends with the same account)
    account_id: uuid.UUID | None = None
    provider: str = "xabarchi"
    #: Eskiz: the cabinet e-mail
    login: str | None = None
    #: Eskiz: the sender name (nick)
    sender: str | None = None


def account_to_sms(account: MessagingAccount | None) -> SmsAccount | None:
    """A usable SMS account, or None: missing, deleted, inactive, `provider='none'` (SMS switched off), no key —
    or an Eskiz row without its login."""
    if account is None or account.deleted_at is not None or not account.is_active or account.provider not in SMS_PROVIDERS or not account.api_key_enc:
        return None
    if account.provider == "eskiz" and not account.login:
        return None
    return SmsAccount(
        key_enc=account.api_key_enc,
        priority=account.default_priority or "transactional",
        unit_id=account.unit_id,
        account_id=account.id,
        provider=account.provider,
        login=account.login,
        sender=account.sender,
    )
