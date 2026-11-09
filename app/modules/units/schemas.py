"""Organisation-unit DTOs — mirror of Clinic-Web `src/domain/org.ts` (OrgUnit, its SMS / Telegram / settings cards).

One object for every level of the tree: country node, region, district, facility. A facility is where cheques are
written; the others group facilities and carry inherited settings."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas import CamelModel

UnitLevel = Literal["country", "region", "district", "facility"]
FacilityKind = Literal["ses", "clinic", "laboratory", "polyclinic", "hospital", "pharmacy", "other"]
SmsProvider = Literal["xabarchi", "none"]
SmsPriority = Literal["urgent", "transactional", "bulk"]
#: inherited = no account of its own (the nearest ancestor's is used) · own = its own Xabarchi key · off = no SMS here and below
UnitSmsMode = Literal["inherited", "own", "off"]
Locale = Literal["uz", "ru", "en"]

#: upper bound of `settings.documentFreeViewDays` — a result link never stays open without a code for good
DOCUMENT_FREE_VIEW_MAX_DAYS = 90


def document_free_view_days(unit_settings: Mapping[str, Any] | None) -> int:
    """`settings.documentFreeViewDays` (effective, merged root → leaf): for how many days after a result is issued its
    SMS link opens the PDF without an SMS code. 0 (the default) = a code is always needed; unreadable counts as 0."""
    try:
        days = int((unit_settings or {}).get("documentFreeViewDays") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(days, DOCUMENT_FREE_VIEW_MAX_DAYS))


class UnitCounts(CamelModel):
    regions: int = 0
    districts: int = 0
    facilities: int = 0
    employees: int = 0


class UnitSettingsOut(CamelModel):
    """Effective settings of the unit (inherited + own) and the raw overrides saved on the unit itself."""

    document_free_view_days: int = 0
    locale: Locale | None = None
    own: dict[str, Any] = Field(default_factory=dict)


class UnitSmsOut(CamelModel):
    mode: UnitSmsMode
    #: what really sends this unit's SMS: xabarchi (a key somewhere up the tree) or none
    provider: SmsProvider = "none"
    api_key_masked: str | None = None
    default_priority: SmsPriority = "transactional"
    sender_note: str | None = None
    #: the unit whose account is in effect (itself, an ancestor, or None when nothing is configured)
    effective_unit_id: str | None = None
    effective_unit_name: str | None = None


class UnitSmsIn(CamelModel):
    """`own` needs a key (a new one, or the one already saved); `inherited` drops this unit's account;
    `off` switches SMS off for this unit and everything under it."""

    mode: UnitSmsMode
    api_key: str | None = Field(default=None, max_length=200)
    default_priority: SmsPriority | None = None
    sender_note: str | None = Field(default=None, max_length=200)


class UnitTelegramOut(CamelModel):
    bot_username: str | None = None
    connected: bool = False
    #: the bot account sits on this very unit (else it is inherited — or the platform bot)
    own: bool = False
    effective_unit_id: str | None = None


class SmsTemplates(BaseModel):
    """SMS text overrides; keys are snake_case on the wire (`payment_receipt`, `result_ready`, `reminder`)."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    payment_receipt: str | None = Field(default=None, max_length=500)
    result_ready: str | None = Field(default=None, max_length=500)
    reminder: str | None = Field(default=None, max_length=500)


class UnitSmsTemplatesOut(CamelModel):
    unit_id: str
    #: the texts this unit's messages use (own, else inherited, else the platform default when a key is empty)
    templates: SmsTemplates
    #: the texts saved on this unit itself
    own: SmsTemplates
    #: the unit has not saved its own texts — an ancestor's are used
    inherited: bool
    #: how many units the save changed (1, or the whole subtree with `applyToAll`)
    applied: int = 0


class UnitSmsTemplatesIn(CamelModel):
    templates: SmsTemplates
    #: write the same texts to every unit under this one too (they stop inheriting)
    apply_to_all: bool = False


class OrgUnitOut(CamelModel):
    id: str
    level: UnitLevel
    kind: FacilityKind | None = None
    parent_id: str | None = None
    country_id: str
    region_id: str | None = None
    district_id: str | None = None
    country_name: str | None = None
    region_name: str | None = None
    district_name: str | None = None
    name: str
    short_name: str | None = None
    code: str | None = None
    legal_name: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    timezone: str
    logo_url: str | None = None
    is_active: bool
    sort_order: int = 0
    #: 0 = country node
    depth: int = 0
    #: "O‘zbekiston Respublikasi › Xorazm viloyati › Bog‘ot tumani › Bog‘ot tumani SES"
    path: str
    settings: UnitSettingsOut
    sms: UnitSmsOut | None = None
    telegram: UnitTelegramOut | None = None
    counts: UnitCounts
    created_at: datetime
    updated_at: datetime


class UnitCreateIn(CamelModel):
    """A child of `parentId` (its level follows from the parent). Without a parent a country node is created
    (`countryId` required; platform permission). Region nodes need `regionId`, district nodes `districtId`;
    a facility inherits the district's geo and needs `kind` + `code`."""

    parent_id: str | None = None
    country_id: str | None = None
    region_id: str | None = None
    district_id: str | None = None
    kind: FacilityKind | None = None
    name: str = Field(min_length=1, max_length=200)
    short_name: str | None = Field(default=None, max_length=80)
    code: str | None = Field(default=None, min_length=1, max_length=12)
    legal_name: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    address: str | None = None
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool = True
    sort_order: int = 0


class UnitUpdateIn(CamelModel):
    """Partial merge — only fields present in the JSON body are applied. Level and parent are immutable."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    short_name: str | None = Field(default=None, max_length=80)
    code: str | None = Field(default=None, min_length=1, max_length=12)
    kind: FacilityKind | None = None
    legal_name: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    address: str | None = None
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    sort_order: int | None = None
    logo_url: str | None = None
    document_free_view_days: int | None = Field(default=None, ge=0, le=DOCUMENT_FREE_VIEW_MAX_DAYS)
    locale: Locale | None = None


class UnitTreeQuery(CamelModel):
    """`GET /units/tree`: every unit the caller may see (their scope + the path above it); superadmin: everything,
    optionally one country or one subtree."""

    country_id: str | None = None
    root_id: str | None = None
    include_inactive: bool = True


class SmsTestIn(CamelModel):
    to: str | None = Field(default=None, max_length=20)


class SmsTestOut(CamelModel):
    ok: bool
    provider_message_id: str | None = None
    to: str | None = None


class TelegramSettingsIn(CamelModel):
    """`botToken` null/empty disconnects the unit's own bot; a value is validated against Telegram `getMe`."""

    bot_token: str | None = Field(default=None, max_length=120)


class ResetPartOut(CamelModel):
    """One part of a reset: what it removes (counts) and which parts it pulls in."""

    key: str
    counts: dict[str, int]
    requires: list[str]


class ResetPreviewOut(CamelModel):
    """What a superadmin reset of the unit (and everything under it) would remove — nothing is changed by the preview."""

    id: str
    name: str
    level: UnitLevel
    #: the word to type to confirm: the facility code, else the unit name
    confirm_word: str
    parts: list[ResetPartOut]


class ResetIn(CamelModel):
    """Parts to reset (their dependencies are added) + the confirmation word."""

    parts: list[str] = Field(min_length=1, max_length=10)
    confirm: str = Field(min_length=1, max_length=200)


class ResetOut(CamelModel):
    parts: list[str]
    counts: dict[str, int]
