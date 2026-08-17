"""Tenant DTOs — mirror of Clinic-Web `src/domain/tenant.ts` (Company, Branch) + backend-only extras
(`telegram`, `smsTemplates`) agreed for the settings pages."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas import CamelModel

SmsProvider = Literal["xabarchi", "none"]
SmsPriority = Literal["urgent", "transactional", "bulk"]
Locale = Literal["uz", "ru", "en"]


class CompanySmsOut(CamelModel):
    provider: SmsProvider
    api_key_masked: str | None = None
    default_priority: SmsPriority
    sender_note: str | None = None


class CompanySmsIn(CamelModel):
    """`apiKey` is write-only plaintext; `apiKeyMasked` is accepted (frontend echoes it) but ignored."""

    provider: SmsProvider = "none"
    api_key: str | None = Field(default=None, max_length=200)
    api_key_masked: str | None = Field(default=None, max_length=80)
    default_priority: SmsPriority = "transactional"
    sender_note: str | None = Field(default=None, max_length=200)


class CompanyTelegramOut(CamelModel):
    bot_username: str | None = None
    connected: bool = False


class SmsTemplates(BaseModel):
    """Per-company SMS text overrides (`companies.settings.smsTemplates`); keys are snake_case on the wire."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    payment_receipt: str | None = Field(default=None, max_length=500)
    result_ready: str | None = Field(default=None, max_length=500)
    reminder: str | None = Field(default=None, max_length=500)


class CompanyOut(CamelModel):
    id: str
    name: str
    legal_name: str | None = None
    slug: str
    logo_url: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    locale: Locale
    is_active: bool
    sms: CompanySmsOut
    telegram: CompanyTelegramOut
    sms_templates: SmsTemplates
    branch_count: int
    employee_count: int
    created_at: datetime
    updated_at: datetime


class CompanyCreateIn(CamelModel):
    name: str = Field(min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=300)
    slug: str | None = Field(default=None, max_length=80)
    logo_url: str | None = None
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    address: str | None = None
    locale: Locale = "uz"
    is_active: bool = True
    sms: CompanySmsIn | None = None
    sms_templates: SmsTemplates | None = None


class CompanyUpdateIn(CamelModel):
    """Partial merge — only fields present in the JSON body are applied."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=300)
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    logo_url: str | None = None
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    address: str | None = None
    locale: Locale | None = None
    is_active: bool | None = None
    sms: CompanySmsIn | None = None
    sms_templates: SmsTemplates | None = None


class BranchOut(CamelModel):
    id: str
    company_id: str
    name: str
    code: str
    address: str | None = None
    phone: str | None = None
    timezone: str
    is_active: bool
    order_seq: int
    created_at: datetime
    updated_at: datetime


class BranchCreateIn(CamelModel):
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=12)
    address: str | None = None
    phone: str | None = Field(default=None, max_length=40)
    timezone: str = Field(default="Asia/Tashkent", max_length=64)
    is_active: bool = True


class BranchUpdateIn(CamelModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, min_length=1, max_length=12)
    address: str | None = None
    phone: str | None = Field(default=None, max_length=40)
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None


class SmsTestIn(CamelModel):
    to: str | None = Field(default=None, max_length=20)


class SmsTestOut(CamelModel):
    ok: bool
    provider_message_id: str | None = None


class TelegramSettingsIn(CamelModel):
    """`botToken` null/empty disconnects the bot; a value is validated against Telegram `getMe`."""

    bot_token: str | None = Field(default=None, max_length=120)
