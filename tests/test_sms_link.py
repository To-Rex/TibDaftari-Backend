"""Result-ready SMS: the {link} placeholder and the public link (pure — no database)."""

from __future__ import annotations

from types import SimpleNamespace

from app.api.deps import RequestMeta
from app.core.config import settings
from app.modules.messaging.service import render_text, result_ready_order_text, result_ready_text
from app.modules.orders.service import public_result_link

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
