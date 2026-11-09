"""SMS texts and accounts down the tree (pure — no database): the nearest unit's text wins per key, a unit without
texts of its own uses its ancestors', and the unit cards report where the account in effect sits."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.infrastructure.db.models import MessagingAccount
from app.modules.messaging.service import render_text
from app.modules.units.repository import merge_texts, nearest_account
from app.modules.units.service import sms_out, telegram_out

COUNTRY, REGION, DISTRICT, FACILITY = (uuid.uuid4() for _ in range(4))
PATH = [COUNTRY, REGION, DISTRICT, FACILITY]


def _unit(unit_id: uuid.UUID, level: str = "facility") -> SimpleNamespace:
    return SimpleNamespace(id=unit_id, level=level, name=f"unit-{str(unit_id)[:4]}")


def _account(unit_id: uuid.UUID, channel: str = "sms", provider: str = "xabarchi", key: str | None = "enc", *, active: bool = True, priority: str = "transactional", note: str | None = None, username: str | None = None) -> MessagingAccount:
    now = datetime.now(UTC)
    return MessagingAccount(id=uuid.uuid4(), unit_id=unit_id, channel=channel, provider=provider, api_key_enc=key, api_key_masked="xab_••••" if key else None, default_priority=priority, sender_note=note, bot_username=username, is_active=active, created_at=now, updated_at=now)


def test_texts_merge_root_to_leaf_and_the_nearest_wins() -> None:
    texts = {
        COUNTRY: {"payment_receipt": "Davlat: {order}", "result_ready": "Davlat: {service} {link}"},
        REGION: {"result_ready": "Viloyat: {service} {link}"},
        FACILITY: {"payment_receipt": "Muassasa chek {order}"},
    }
    merged = merge_texts(PATH, texts)
    assert merged == {"payment_receipt": "Muassasa chek {order}", "result_ready": "Viloyat: {service} {link}"}
    # a unit above the facility sees only what is above it
    assert merge_texts(PATH[:3], texts) == {"payment_receipt": "Davlat: {order}", "result_ready": "Viloyat: {service} {link}"}
    assert merge_texts([], texts) == {}
    # rendering with the merged texts: the facility's own receipt, the region's result text, the default for the rest
    assert render_text(merged, "payment_receipt", order="UR-1", org="Xorazm SES") == "Muassasa chek UR-1"
    assert render_text(merged, "result_ready", service="Qon", link="L") == "Viloyat: Qon L"
    assert render_text(merged, "result_ready_order", service="Virus", count=2, link="L") == "Viloyat: Virus L"  # falls back to the result text
    assert render_text(merged, "reminder", patient="Ali", org="Xorazm SES", order="UR-1") == "Hurmatli Ali! Sizni Xorazm SES klinikasida kutamiz. Chek: UR-1"


def test_unit_placeholder_is_the_facility_and_org_the_organisation() -> None:
    texts = {"payment_receipt": "Chek {order} — {unit}, {org}"}
    assert render_text(texts, "payment_receipt", order="UR-7", org="Xorazm SES", unit="Bog‘ot SES") == "Chek UR-7 — Bog‘ot SES, Xorazm SES"
    # a text saved before the tree keeps working through the aliases
    assert render_text({"payment_receipt": "Chek {order} — {branch}, {company}"}, "payment_receipt", order="UR-7", org="Xorazm SES", unit="Bog‘ot SES") == "Chek UR-7 — Bog‘ot SES, Xorazm SES"
    # amounts: this payment, the cheque total and what is still to pay
    text = render_text({"payment_receipt": "Chek {order}: {amount} so'm to'landi, jami {total}, qoldiq {remaining}. {org}"}, "payment_receipt", order="UR-9", amount="120 000", total="150 000", remaining="30 000", org="Temo Med")
    assert text == "Chek UR-9: 120 000 so'm to'landi, jami 150 000, qoldiq 30 000. Temo Med"
    assert render_text({}, "payment_receipt", order="UR-9", amount="5 000", org="Temo Med").startswith("Chek UR-9: 5 000 so‘m qabul qilindi.")


def test_nearest_account_on_the_path() -> None:
    region = _account(REGION)
    facility_off = _account(FACILITY, provider="none", key=None)
    inactive = _account(DISTRICT, active=False)
    assert nearest_account(PATH, {REGION: region}) is region
    assert nearest_account(PATH, {REGION: region, DISTRICT: inactive}) is region  # inactive rows do not count
    assert nearest_account(PATH, {REGION: region, FACILITY: facility_off}) is facility_off  # "none" stops the inheritance
    assert nearest_account(PATH, {}) is None
    assert nearest_account(PATH[:2], {FACILITY: _account(FACILITY)}) is None  # a unit below is not on the path


def test_sms_card_reports_mode_and_where_the_key_comes_from() -> None:
    region = _account(REGION, priority="urgent", note="n")
    names = {REGION: "Xorazm viloyati", FACILITY: "Bog‘ot SES"}
    inherited = sms_out(_unit(FACILITY), PATH, {REGION: region}, names)
    assert (inherited.mode, inherited.provider, inherited.api_key_masked, inherited.default_priority, inherited.effective_unit_id, inherited.effective_unit_name) == ("inherited", "xabarchi", "xab_••••", "urgent", str(REGION), "Xorazm viloyati")
    own = _account(FACILITY, key="own-enc", priority="bulk")
    card = sms_out(_unit(FACILITY), PATH, {REGION: region, FACILITY: own}, names)
    assert (card.mode, card.provider, card.default_priority, card.effective_unit_id) == ("own", "xabarchi", "bulk", str(FACILITY))
    off = sms_out(_unit(FACILITY), PATH, {REGION: region, FACILITY: _account(FACILITY, provider="none", key=None)}, names)
    assert (off.mode, off.provider, off.api_key_masked, off.effective_unit_id) == ("off", "none", None, None)
    nothing = sms_out(_unit(FACILITY), PATH, {}, names)
    assert (nothing.mode, nothing.provider, nothing.effective_unit_id) == ("inherited", "none", None)
    assert "own-enc" not in card.model_dump_json()


def test_telegram_card_reports_the_bot_in_effect() -> None:
    bot = _account(REGION, channel="telegram", provider="telegram", key="tok", username="region_bot")
    card = telegram_out(_unit(FACILITY), PATH, {REGION: bot})
    assert (card.connected, card.bot_username, card.own, card.effective_unit_id) == (True, "region_bot", False, str(REGION))
    mine = telegram_out(_unit(REGION, "region"), PATH[:2], {REGION: bot})
    assert mine.own is True and mine.effective_unit_id == str(REGION)
    none = telegram_out(_unit(FACILITY), PATH, {})
    assert (none.connected, none.bot_username, none.own, none.effective_unit_id) == (False, None, False, None)
