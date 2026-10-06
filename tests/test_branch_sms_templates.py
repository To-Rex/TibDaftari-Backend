"""SMS texts per branch (pure — no database): a branch's own texts win, a branch without them uses the company's."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.modules.messaging.service import (
    payment_receipt_text,
    render_text,
    result_ready_order_text,
    result_ready_text,
    sms_overrides,
)
from app.modules.tenant.service import branch_sms_templates_out

COMPANY = SimpleNamespace(name="Temo Med", settings={"smsTemplates": {"result_ready": "Kompaniya: {service} {link}"}})
INHERITS = SimpleNamespace(id=uuid.uuid4(), settings={})
OWN = SimpleNamespace(id=uuid.uuid4(), settings={"smsTemplates": {"result_ready": "Filial: {service} {link}", "payment_receipt": "Filial chek {order}"}})
DEFAULTS = SimpleNamespace(id=uuid.uuid4(), settings={"smsTemplates": {}})  # saved "everything default"


def test_branch_without_own_texts_uses_the_company_ones() -> None:
    assert result_ready_text(COMPANY, "Qon", link="L", branch=INHERITS) == "Kompaniya: Qon L"
    assert result_ready_text(COMPANY, "Qon", link="L") == "Kompaniya: Qon L"  # no branch: as before


def test_branch_own_texts_win_and_stay_in_that_branch() -> None:
    assert result_ready_text(COMPANY, "Qon", link="L", branch=OWN) == "Filial: Qon L"
    assert payment_receipt_text(COMPANY, "UR-1", 1000, branch=OWN) == "Filial chek UR-1"
    # the order-scope text falls back to the branch's own result text, not the company's
    assert result_ready_order_text(COMPANY, "Virus", 2, link="L", branch=OWN) == "Filial: Virus L"
    # another branch is not affected
    assert result_ready_text(COMPANY, "Qon", link="L", branch=INHERITS) == "Kompaniya: Qon L"


def test_branch_that_saved_defaults_ignores_the_company_override() -> None:
    assert sms_overrides(COMPANY, DEFAULTS) == {}
    assert render_text(COMPANY, "result_ready", branch=DEFAULTS, service="Qon", link="L") == "Qon natijasi tayyor: L Temo Med"


def test_branch_dto_reports_inheritance() -> None:
    out = branch_sms_templates_out(INHERITS, COMPANY)  # type: ignore[arg-type]
    assert out.inherited is True and out.templates.result_ready == "Kompaniya: {service} {link}"
    own = branch_sms_templates_out(OWN, COMPANY, applied=1)  # type: ignore[arg-type]
    assert own.inherited is False and own.templates.payment_receipt == "Filial chek {order}" and own.applied == 1
    body = own.model_dump(by_alias=True)
    assert body["branchId"] == str(OWN.id) and "payment_receipt" in body["templates"]
