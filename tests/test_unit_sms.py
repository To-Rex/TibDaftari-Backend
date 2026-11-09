"""Each unit sends SMS with the nearest Xabarchi account on its path (test database, rows prefixed T-sms-):
a facility inherits its region's key, may have its own, or switch SMS off for itself; the dispatcher sends every
message with the key it was queued with."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.crypto import decrypt, encrypt
from app.infrastructure.db.models import MessagingAccount
from app.modules.messaging import dispatcher, xabarchi
from app.modules.messaging import repository as msg_repo
from app.modules.messaging.service import enqueue, enqueue_sms_if_configured, sms_account_for
from app.modules.messaging.xabarchi import ProviderResult
from fastapi.testclient import TestClient

from tests.helpers import API, app_instance, bearer, db, make_employee, make_facility, make_tree, seed, staff_token

SFX = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        second = await make_facility(s, a.district, name=f"T-{a.tag} Ikkinchi")
        region_acc = MessagingAccount(unit_id=a.region.id, channel="sms", provider="xabarchi", api_key_enc=encrypt("region-key"), api_key_masked="regi••••-key", default_priority="urgent")
        s.add(region_acc)
        admin = await make_employee(s, a.region, "admin", login=f"t-sms-admin-{SFX}")
        await s.flush()
        return {
            "region": str(a.region.id),
            "district": str(a.district.id),
            "facility": str(a.facility.id),
            "second": str(second.id),
            "region_account": str(region_acc.id),
            "admin": admin.login,
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["h"] = bearer(staff_token(client, data["admin"]))
        yield data


def test_facility_inherits_the_region_key_or_uses_its_own(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    fac, district, region, second = (uuid.UUID(ctx[k]) for k in ("facility", "district", "region", "second"))

    async def inherited(s: Any):
        return await sms_account_for(s, fac), await sms_account_for(s, district), await sms_account_for(s, None)

    acc_fac, acc_district, none = db(c, inherited)
    assert none is None
    assert decrypt(acc_fac.key_enc) == "region-key" and acc_fac.priority == "urgent" and acc_fac.unit_id == region and acc_fac.account_id == uuid.UUID(ctx["region_account"])
    assert acc_district.account_id == acc_fac.account_id
    # the facility's own key wins for the facility only; the sibling facility still inherits
    r = c.put(f"{API}/units/{fac}/sms", json={"mode": "own", "apiKey": " fac-key ", "defaultPriority": "bulk"}, headers=ctx["h"])
    assert r.status_code == 200 and r.json()["mode"] == "own" and "fac-key" not in r.text, r.text

    async def own(s: Any):
        return await sms_account_for(s, fac), await sms_account_for(s, second)

    acc_fac, acc_second = db(c, own)
    assert decrypt(acc_fac.key_enc) == "fac-key" and acc_fac.priority == "bulk" and acc_fac.unit_id == fac
    assert decrypt(acc_second.key_enc) == "region-key" and acc_second.unit_id == region
    # changing only the priority keeps the saved key
    r = c.put(f"{API}/units/{fac}/sms", json={"mode": "own", "defaultPriority": "transactional"}, headers=ctx["h"])
    assert r.status_code == 200 and r.json()["apiKeyMasked"] and r.json()["defaultPriority"] == "transactional"
    assert decrypt(db(c, lambda s: sms_account_for(s, fac)).key_enc) == "fac-key"


def test_switched_off_facility_sends_nothing_and_records_it(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    fac, second = uuid.UUID(ctx["facility"]), uuid.UUID(ctx["second"])
    assert c.put(f"{API}/units/{fac}/sms", json={"mode": "off"}, headers=ctx["h"]).json()["provider"] == "none"

    async def flow(s: Any):
        assert await sms_account_for(s, fac) is None
        off = await enqueue_sms_if_configured(s, fac, kind="payment_receipt", to="998901234567", text="t")
        on = await enqueue_sms_if_configured(s, second, kind="payment_receipt", to="998901234567", text="t")
        bad = await enqueue_sms_if_configured(s, second, kind="payment_receipt", to="12345", text="t")
        secret = await enqueue_sms_if_configured(s, fac, kind="otp", to="998901234567", text="Kod: 1234")
        return off, on, bad, secret

    off, on, bad, secret = db(c, flow)
    assert off is not None and off.status == "failed" and off.error == "sms_not_configured" and off.account_id is None
    assert on is not None and on.status == "queued" and on.account_id == uuid.UUID(ctx["region_account"]) and on.unit_id == second
    assert bad is None and secret is None  # an invalid phone / a credential text without a provider is never stored
    # back to the inherited key
    assert c.put(f"{API}/units/{fac}/sms", json={"mode": "inherited"}, headers=ctx["h"]).json()["provider"] == "xabarchi"


def test_the_dispatcher_uses_each_message_account_key(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    fac, second = uuid.UUID(ctx["facility"]), uuid.UUID(ctx["second"])
    calls: list[tuple[str, str]] = []

    async def fake_send(api_key: str, to: list[str], text: str, priority: str = "transactional") -> list[ProviderResult]:
        calls.append((api_key, priority))
        return [ProviderResult(to=to[0], provider_id="1", status="queued", raw={})]

    monkeypatch.setattr(xabarchi, "send_sms", fake_send)
    assert c.put(f"{API}/units/{fac}/sms", json={"mode": "own", "apiKey": "fac-key-2", "defaultPriority": "bulk"}, headers=ctx["h"]).status_code == 200

    async def flow(s: Any) -> list[str]:
        own = await enqueue(s, unit_id=fac, channel="sms", kind="payment_receipt", to="998901234567", text="t")
        shared = await enqueue(s, unit_id=second, channel="sms", kind="payment_receipt", to="998901234567", text="t")
        nobody = await enqueue(s, unit_id=None, channel="sms", kind="broadcast", to="998901234567", text="t")
        # a message queued with an account that was switched off since: the dispatcher falls back to what is in
        # effect for its facility now (the region's key)
        stale = await enqueue(s, unit_id=fac, channel="sms", kind="payment_receipt", to="998901234567", text="t")
        assert own.account_id != shared.account_id and stale.account_id == own.account_id and nobody.account_id is None
        accounts = await msg_repo.load_accounts(s, {own.account_id, shared.account_id})
        out = []
        for m in (own, shared, nobody):
            m.status = "sending"
            out.append(await dispatcher.deliver_one(s, m, accounts))
        assert nobody.error == "sms_not_configured"
        # the facility's account is switched off after the message was queued
        fac_account = await s.get(MessagingAccount, own.account_id)
        fac_account.is_active = False
        await s.flush()
        stale.status = "sending"
        out.append(await dispatcher.deliver_one(s, stale, None))
        fac_account.is_active = True
        return out

    assert db(c, flow) == ["sent", "sent", "failed", "sent"]
    assert calls == [("fac-key-2", "bulk"), ("region-key", "urgent"), ("region-key", "urgent")]
    assert c.put(f"{API}/units/{fac}/sms", json={"mode": "inherited"}, headers=ctx["h"]).status_code == 200
