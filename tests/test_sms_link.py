"""Result-ready SMS: the {link} placeholder, the public links and the SMS account rules (pure — no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.api.deps import RequestMeta
from app.core.config import settings
from app.infrastructure.db.models import MessagingAccount
from app.modules.messaging.service import account_to_sms, render_text
from app.modules.orders.service import (
    document_qr_link,
    mask_name,
    mask_phone,
    public_code_free,
    public_free_until,
    public_result_link,
    public_verify_link_for_token,
)
from app.modules.units.schemas import DOCUMENT_FREE_VIEW_MAX_DAYS, document_free_view_days

ORG = "Temo Med"


def test_default_texts_carry_the_link() -> None:
    assert render_text({}, "result_ready", org=ORG, service="Qon tahlili", link="https://temo.uz/d/abc") == "Qon tahlili natijasi tayyor: https://temo.uz/d/abc Temo Med"
    assert render_text({}, "result_ready_order", org=ORG, service="Virusologiya", count=3, link="https://temo.uz/d/x") == "Virusologiya: 3 ta tahlil natijasi tayyor: https://temo.uz/d/x Temo Med"
    # no link: no double space left behind
    assert "  " not in render_text({}, "result_ready", org=ORG, service="Qon tahlili")


def test_unit_override_can_use_the_link() -> None:
    texts = {"result_ready": "Hurmatli {patient}! Natija: {link}"}
    assert render_text(texts, "result_ready", patient="Ali", link="https://t.uz/d/1") == "Hurmatli Ali! Natija: https://t.uz/d/1"
    # an override without {link} stays exactly as written
    assert render_text({"result_ready": "{service} tayyor."}, "result_ready", service="X", link="https://t.uz/d/1") == "X tayyor."
    # a customised result text also serves the order-scope approval
    assert render_text({"result_ready": "{service}: {link}"}, "result_ready_order", service="Panel", count=2, link="L") == "Panel: L"


def test_org_and_unit_placeholders_and_their_aliases() -> None:
    texts = {"payment_receipt": "Chek {order} — {unit}, {org}"}
    assert render_text(texts, "payment_receipt", order="UR-7", org="Xorazm SES", unit="Bog‘ot SES") == "Chek UR-7 — Bog‘ot SES, Xorazm SES"
    # texts written before the tree keep working: {company} = {org}, {branch} = {unit}
    old = {"payment_receipt": "Chek {order} — {branch}, {company}"}
    assert render_text(old, "payment_receipt", order="UR-7", org="Xorazm SES", unit="Bog‘ot SES") == "Chek UR-7 — Bog‘ot SES, Xorazm SES"
    # and callers may still pass the old names
    assert render_text(texts, "payment_receipt", order="UR-7", company="Xorazm SES", branch="Bog‘ot SES") == "Chek UR-7 — Bog‘ot SES, Xorazm SES"
    # without a unit the placeholder is just dropped (no double space left behind)
    assert render_text({"result_ready": "{service} tayyor {unit} {link}"}, "result_ready", service="Qon", link="L") == "Qon tayyor L"


def test_public_link_uses_the_staff_web_app() -> None:
    doc = SimpleNamespace(public_token="tok123")
    meta = RequestMeta(ip=None, request_id=None, user_agent=None, origin="https://temo.uz")
    assert public_result_link(doc, meta) == "https://temo.uz/d/tok123"
    assert public_result_link(doc, None) == settings.frontend_url.rstrip("/") + "/d/tok123"
    assert public_result_link(SimpleNamespace(public_token=None), meta) == ""


def test_qr_gets_its_own_verification_link() -> None:
    meta = RequestMeta(ip=None, request_id=None, user_agent=None, origin="https://temo.uz")
    assert public_verify_link_for_token("vtok", meta) == "https://temo.uz/v/vtok"
    # the QR value of a document is whatever was frozen into its snapshot (old documents: the SMS link)
    assert document_qr_link(SimpleNamespace(snapshot={"context": {"doc": {"link": "https://temo.uz/v/vtok"}}})) == "https://temo.uz/v/vtok"
    assert document_qr_link(SimpleNamespace(snapshot={"context": {"doc": {"link": "https://temo.uz/d/old"}}})) == "https://temo.uz/d/old"
    assert document_qr_link(SimpleNamespace(snapshot={})) is None
    assert document_qr_link(SimpleNamespace(snapshot={"context": {"doc": None}})) is None


def _account(provider: str = "xabarchi", key: str | None = "enc-key", *, active: bool = True, deleted: bool = False, priority: str = "transactional") -> MessagingAccount:
    now = datetime.now(UTC)
    return MessagingAccount(id=uuid.uuid4(), unit_id=uuid.uuid4(), channel="sms", provider=provider, api_key_enc=key, default_priority=priority, is_active=active, created_at=now, updated_at=now, deleted_at=now if deleted else None)


def test_only_a_live_xabarchi_account_with_a_key_can_send() -> None:
    """The nearest account on the path decides: `provider='none'` switches SMS off for the subtree — the caller must
    then say so (never a silent "code sent")."""
    acc = _account(priority="urgent")
    sms = account_to_sms(acc)
    assert sms is not None and sms.key_enc == "enc-key" and sms.priority == "urgent" and sms.unit_id == acc.unit_id and sms.account_id == acc.id
    assert account_to_sms(None) is None
    assert account_to_sms(_account("none", None)) is None
    assert account_to_sms(_account(key=None)) is None
    assert account_to_sms(_account(active=False)) is None
    assert account_to_sms(_account(deleted=True)) is None
    assert account_to_sms(_account(priority="")).priority == "transactional"  # type: ignore[union-attr]


def test_mask_name_keeps_only_initials() -> None:
    assert mask_name("Haydarov Dilshodjon") == "H*** D***"
    assert mask_name("  aliyev  vali  akmal o‘g‘li ") == "A*** V*** A***"
    assert mask_name("") == "" and mask_name(None) == ""


def test_mask_phone_hides_the_middle() -> None:
    assert mask_phone("998995340313") == "+998 ** *** 03 13"
    assert mask_phone("+998 99 534 03 13") == "+998 ** *** 03 13"
    assert mask_phone("123") is None
    assert mask_phone(None) is None


def test_code_free_days_setting_is_read_defensively() -> None:
    assert document_free_view_days(None) == 0
    assert document_free_view_days({}) == 0
    assert document_free_view_days({"documentFreeViewDays": 7}) == 7
    assert document_free_view_days({"documentFreeViewDays": "3"}) == 3
    # junk or out-of-range values never open links for good
    assert document_free_view_days({"documentFreeViewDays": "x"}) == 0
    assert document_free_view_days({"documentFreeViewDays": -5}) == 0
    assert document_free_view_days({"documentFreeViewDays": 10_000}) == DOCUMENT_FREE_VIEW_MAX_DAYS


def test_code_free_window_runs_from_the_issue_date() -> None:
    issued = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
    assert public_free_until(issued, 0) is None
    assert public_free_until(issued, 3) == issued + timedelta(days=3)
    # 0 days: a code is always needed
    assert public_code_free(issued, 0, issued) is None
    # inside the window the link opens directly; the last second still counts, the end itself does not
    assert public_code_free(issued, 3, issued + timedelta(days=2)) == issued + timedelta(days=3)
    assert public_code_free(issued, 3, issued + timedelta(days=3, seconds=-1)) is not None
    assert public_code_free(issued, 3, issued + timedelta(days=3)) is None
    assert public_code_free(issued, 7, issued + timedelta(days=30)) is None
