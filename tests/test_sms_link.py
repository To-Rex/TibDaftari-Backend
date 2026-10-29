"""Result-ready SMS: the {link} placeholder and the public link (pure — no database)."""

from __future__ import annotations

from types import SimpleNamespace

from app.api.deps import RequestMeta
from app.core.config import settings
from app.modules.messaging.service import (
    render_text,
    result_ready_order_text,
    result_ready_text,
    sms_branch_for,
)
from app.modules.orders.service import (
    document_qr_link,
    mask_name,
    mask_phone,
    public_result_link,
    public_verify_link_for_token,
)

COMPANY = SimpleNamespace(name="Temo Med", settings={})


def test_default_texts_carry_the_link() -> None:
    assert result_ready_text(COMPANY, "Qon tahlili", link="https://temo.uz/d/abc") == "Qon tahlili natijasi tayyor: https://temo.uz/d/abc Temo Med"
    assert result_ready_order_text(COMPANY, "Virusologiya", 3, link="https://temo.uz/d/x") == "Virusologiya: 3 ta tahlil natijasi tayyor: https://temo.uz/d/x Temo Med"
    # no link: no double space left behind
    assert "  " not in result_ready_text(COMPANY, "Qon tahlili")


def test_company_override_can_use_the_link() -> None:
    company = SimpleNamespace(name="Temo Med", settings={"smsTemplates": {"result_ready": "Hurmatli {patient}! Natija: {link}"}})
    assert render_text(company, "result_ready", patient="Ali", link="https://t.uz/d/1") == "Hurmatli Ali! Natija: https://t.uz/d/1"
    # an override without {link} stays exactly as written
    company2 = SimpleNamespace(name="Temo Med", settings={"smsTemplates": {"result_ready": "{service} tayyor."}})
    assert render_text(company2, "result_ready", service="X", link="https://t.uz/d/1") == "X tayyor."


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


def test_public_link_code_goes_out_through_the_first_branch_that_can_send() -> None:
    company = SimpleNamespace(sms_provider="xabarchi", sms_api_key_enc="co-key", sms_default_priority="transactional")
    no_key_company = SimpleNamespace(sms_provider="none", sms_api_key_enc=None, sms_default_priority="transactional")
    shared = SimpleNamespace(sms_provider=None, sms_api_key_enc=None, sms_default_priority=None)  # on the company's key
    own = SimpleNamespace(sms_provider="xabarchi", sms_api_key_enc="br-key", sms_default_priority=None)  # its own key
    off = SimpleNamespace(sms_provider="none", sms_api_key_enc=None, sms_default_priority=None)  # switched SMS off
    # the cheque's branch sends; one that switched SMS off is skipped in favour of the patient's branch
    assert sms_branch_for(company, shared, own) == (shared, True)
    assert sms_branch_for(company, off, own) == (own, True)
    assert sms_branch_for(no_key_company, shared, own) == (own, True)
    # nobody can send → the caller must say so (never a silent "code sent")
    assert sms_branch_for(company, off) == (None, False)
    assert sms_branch_for(company, off, off) == (None, False)
    assert sms_branch_for(no_key_company, shared) == (None, False)
    # no branch at all (missing rows) → the company's key decides
    assert sms_branch_for(company) == (None, True)
    assert sms_branch_for(company, None, None) == (None, True)
    assert sms_branch_for(no_key_company, None) == (None, False)


def test_mask_name_keeps_only_initials() -> None:
    assert mask_name("Haydarov Dilshodjon") == "H*** D***"
    assert mask_name("  aliyev  vali  akmal o‘g‘li ") == "A*** V*** A***"
    assert mask_name("") == "" and mask_name(None) == ""


def test_mask_phone_hides_the_middle() -> None:
    assert mask_phone("998995340313") == "+998 ** *** 03 13"
    assert mask_phone("+998 99 534 03 13") == "+998 ** *** 03 13"
    assert mask_phone("123") is None
    assert mask_phone(None) is None
