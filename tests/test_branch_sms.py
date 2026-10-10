"""Each branch sends SMS with its own Xabarchi key; without one, the company's shared key (pure — no database)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from app.core.crypto import decrypt, encrypt
from app.core.exceptions import ValidationError
from app.infrastructure.db.models import Branch, Company, OutboxMessage
from app.modules.messaging import dispatcher, xabarchi
from app.modules.messaging.service import enqueue_sms_if_configured, sms_account
from app.modules.messaging.xabarchi import ProviderResult
from app.modules.tenant.schemas import BranchSmsIn
from app.modules.tenant.service import apply_branch_sms, branch_out, branch_sms_mode, branch_sms_out


def _company(key: str | None = "company-key", provider: str = "xabarchi") -> Company:
    now = datetime.now(UTC)
    return Company(id=uuid.uuid4(), name="Klinika", slug="k", sms_provider=provider if key else "none", sms_api_key_enc=encrypt(key) if key else None,
                   sms_api_key_masked="comp••••-key" if key else None, sms_default_priority="transactional", settings={}, created_at=now, updated_at=now)


def _branch(company: Company, name: str = "Filial", key: str | None = None, mode: str | None = None, priority: str | None = None) -> Branch:
    now = datetime.now(UTC)
    return Branch(id=uuid.uuid4(), company_id=company.id, name=name, code=name[:2].upper(), timezone="Asia/Tashkent", is_active=True, order_seq=0, settings={},
                  sms_provider=mode, sms_api_key_enc=encrypt(key) if key else None, sms_api_key_masked=f"{key[:3]}••••" if key else None,
                  sms_default_priority=priority, created_at=now, updated_at=now)


def _key(account) -> str | None:
    return decrypt(account.key_enc) if account else None


def test_a_branch_without_its_own_key_keeps_the_company_key() -> None:
    company = _company()
    branch = _branch(company)
    assert _key(sms_account(company, branch)) == "company-key" and sms_account(company, branch).source == "company"
    assert _key(sms_account(company, None)) == "company-key"  # messages without a branch (e.g. broadcasts)
    assert branch_sms_mode(branch) == "company"


def test_every_branch_sends_with_its_own_key() -> None:
    company = _company()
    a = _branch(company, "Alfa", key="alfa-key", mode="xabarchi", priority="urgent")
    b = _branch(company, "Beta", key="beta-key", mode="xabarchi")
    assert _key(sms_account(company, a)) == "alfa-key" and sms_account(company, a).priority == "urgent"
    assert _key(sms_account(company, b)) == "beta-key" and sms_account(company, b).priority == "transactional"  # company's priority
    assert sms_account(company, a).source == "branch"
    # a branch key works even when the company itself has no key
    bare = _company(key=None)
    c = _branch(bare, "Gamma", key="gamma-key", mode="xabarchi")
    assert _key(sms_account(bare, c)) == "gamma-key"
    assert sms_account(bare, _branch(bare)) is None


def test_a_branch_switched_off_sends_nothing() -> None:
    company = _company()
    assert sms_account(company, _branch(company, mode="none")) is None


def test_saving_one_branch_never_touches_another_or_the_company() -> None:
    company = _company()
    a = _branch(company, "Alfa")
    b = _branch(company, "Beta", key="beta-key", mode="xabarchi")
    apply_branch_sms(a, BranchSmsIn(mode="own", api_key=" alfa-key "))
    assert decrypt(a.sms_api_key_enc) == "alfa-key" and a.sms_provider == "xabarchi" and "alfa-key" not in (a.sms_api_key_masked or "")
    assert decrypt(b.sms_api_key_enc) == "beta-key" and decrypt(company.sms_api_key_enc) == "company-key"
    # changing only the priority keeps the saved key
    apply_branch_sms(a, BranchSmsIn(mode="own", default_priority="bulk"))
    assert decrypt(a.sms_api_key_enc) == "alfa-key" and a.sms_default_priority == "bulk"
    # back to the shared key / off: the branch's own key is dropped
    apply_branch_sms(a, BranchSmsIn(mode="company"))
    assert a.sms_provider is None and a.sms_api_key_enc is None and _key(sms_account(company, a)) == "company-key"
    apply_branch_sms(b, BranchSmsIn(mode="off"))
    assert b.sms_provider == "none" and b.sms_api_key_enc is None and sms_account(company, b) is None


def test_own_mode_needs_a_key() -> None:
    company = _company()
    with pytest.raises(ValidationError):
        apply_branch_sms(_branch(company), BranchSmsIn(mode="own"))


def test_dtos_show_masks_and_where_sms_go_out_from() -> None:
    company = _company()
    own = _branch(company, "Alfa", key="alfa-key", mode="xabarchi")
    shared = _branch(company, "Beta")
    off = _branch(company, "Gamma", mode="none")
    assert (branch_sms_out(own, company).mode, branch_sms_out(own, company).effective) == ("own", "branch")
    assert (branch_sms_out(shared, company).mode, branch_sms_out(shared, company).effective) == ("company", "company")
    assert (branch_sms_out(off, company).mode, branch_sms_out(off, company).effective) == ("off", None)
    assert branch_sms_out(shared, company).company_api_key_masked == "comp••••-key"
    dumped = branch_sms_out(own, company).model_dump_json() + branch_out(own).model_dump_json()
    assert "alfa-key" not in dumped and branch_out(own).sms_mode == "own" and branch_out(shared).sms_api_key_masked is None


def test_the_dispatcher_uses_each_message_branch_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_send(api_key: str, to: list[str], text: str, priority: str = "transactional") -> list[ProviderResult]:
        calls.append((api_key, priority))
        return [ProviderResult(to=to[0], provider_id="1", status="queued", raw={})]

    monkeypatch.setattr(xabarchi, "send_sms", fake_send)
    company = _company()
    a = _branch(company, "Alfa", key="alfa-key", mode="xabarchi", priority="urgent")
    b = _branch(company, "Beta", key="beta-key", mode="xabarchi")
    shared = _branch(company, "Gamma")
    off = _branch(company, "Delta", mode="none")

    def msg(branch: Branch | None) -> OutboxMessage:
        return OutboxMessage(company_id=company.id, branch_id=branch.id if branch else None, channel="sms", kind="payment_receipt", to="998901234567", text="t", status="sending", attempts=0, payload={})

    async def run() -> list[str]:
        out = []
        for br in (a, b, shared, None, off):
            m = msg(br)
            out.append(await dispatcher.deliver_one(None, m, company, br))  # type: ignore[arg-type]
            if br is off:
                assert m.error == "sms_not_configured"
        return out

    assert asyncio.run(run()) == ["sent", "sent", "sent", "sent", "failed"]
    assert calls == [("alfa-key", "urgent"), ("beta-key", "transactional"), ("company-key", "transactional"), ("company-key", "transactional")]


def test_enqueue_records_a_failure_only_when_the_branch_has_no_key_at_all() -> None:
    bare = _company(key=None)
    own = _branch(bare, "Alfa", key="alfa-key", mode="xabarchi")
    shared = _branch(bare, "Beta")

    class FakeSession:
        def __init__(self) -> None:
            self.rows: list[OutboxMessage] = []

        async def get(self, model, ident):
            return {own.id: own, shared.id: shared}.get(ident)

        def add(self, row: OutboxMessage) -> None:
            self.rows.append(row)

        async def flush(self) -> None:
            return None

    async def run() -> tuple[OutboxMessage | None, OutboxMessage | None]:
        s = FakeSession()
        m1 = await enqueue_sms_if_configured(s, bare, kind="payment_receipt", to="998901234567", text="t", branch_id=own.id)  # type: ignore[arg-type]
        m2 = await enqueue_sms_if_configured(s, bare, kind="payment_receipt", to="998901234567", text="t", branch_id=shared.id)  # type: ignore[arg-type]
        return m1, m2

    m1, m2 = asyncio.run(run())
    assert m1 is not None and m1.status == "queued"
    assert m2 is not None and m2.status == "failed" and m2.error == "sms_not_configured"
