"""Telegram module tests: pure helpers, the link flow + delivery against the test database (rows prefixed
T-telegram-), status / webhook endpoints. No live Telegram calls — the bot is a fake object.

Bots are `messaging_accounts` rows (channel `telegram`): tree A's region has its own bot token, tree B's region
runs on the platform bot (no token stored) and carries a webhook secret in its settings."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.config import settings
from app.core.crypto import encrypt
from app.core.security import sha256_hex
from app.infrastructure.db.models import (
    MessagingAccount,
    OtpChallenge,
    OutboxMessage,
    Patient,
    ResultTemplate,
    TelegramLink,
)
from app.modules.messaging import service as messaging
from app.modules.telegram import repository as tg_repo
from app.modules.telegram import service, texts
from app.modules.telegram.manager import bot_manager
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.helpers import API, COUNTRY_ID, app_instance, bearer, db, make_employee, make_tree, seed, staff_token

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-telegram-pass-1"
PHONE_A = "9989" + f"{int(SFX, 16) % 10**8:08d}"  # 12 digits, unique per run
CHAT = str(10**9 + int(SFX, 16) % 10**8)
TOKEN_A = "123456:T-telegram-token"


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        b.region.settings = {"telegramWebhookSecret": f"whsec-{SFX}"}
        bot_a = MessagingAccount(unit_id=a.region.id, channel="telegram", provider="telegram", api_key_enc=encrypt(TOKEN_A), api_key_masked="123456:••••oken", bot_username="t_telegram_bot")
        bot_b = MessagingAccount(unit_id=b.region.id, channel="telegram", provider="telegram", api_key_enc=None, bot_username=None)
        sms_a = MessagingAccount(unit_id=a.region.id, channel="sms", provider="xabarchi", api_key_enc=encrypt("t-telegram-sms-key"), api_key_masked="t-te••••-key")
        s.add_all([bot_a, bot_b, sms_a])
        emp_a = await make_employee(s, a.region, "admin", login=f"t-telegram-admin-a-{SFX}", password=PASSWORD)
        await make_employee(s, b.region, "admin", login=f"t-telegram-admin-b-{SFX}", password=PASSWORD)
        pat_a = Patient(country_id=COUNTRY_ID, registered_unit_id=a.facility.id, full_name="T-telegram Bemor A", phone=PHONE_A, passport_number=f"TG{SFX}A", tags=[])
        tpl = ResultTemplate(owner_unit_id=a.region.id, name=f"T-telegram tpl {SFX}", status="active", service_type_ids=[], category_ids=[], doc={"elements": []})
        s.add_all([pat_a, tpl])
        await s.flush()
        return {
            "a": {k: str(getattr(a, k).id) for k in ("country", "region", "district", "facility")},
            "b": {k: str(getattr(b, k).id) for k in ("country", "region", "district", "facility")},
            "tag": a.tag,
            "code_a": a.facility.code,
            "bot_a": str(bot_a.id),
            "bot_b": str(bot_b.id),
            "emp_a": str(emp_a.id),
            "pat_a": str(pat_a.id),
            "tpl": str(tpl.id),
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, f"t-telegram-{who}-{SFX}", PASSWORD) for who in ("admin-a", "admin-b")}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


# ----------------------------------------------------------------------------- pure helpers


def test_parse_phone_variants() -> None:
    assert service.parse_phone("+998 90 123-45-67") == "998901234567"
    assert service.parse_phone("901234567") == "998901234567"
    assert service.parse_phone("998901234567") == "998901234567"
    assert service.parse_phone("00998901234567") == "998901234567"
    assert service.parse_phone("12345") is None
    assert service.parse_phone("") is None
    assert service.parse_phone(None) is None


def test_texts_and_button_regex() -> None:
    import re

    assert texts.t("ru", "wrong_code", attempts=2) == "❌ Неверный код. Осталось попыток: 2. Введите ещё раз:"
    assert texts.t("xx", "som") == "so'm"  # unknown lang → uz
    assert texts.t("en", "missing_key") == "missing_key"
    assert texts.norm_lang(None) == "uz" and texts.norm_lang("uzc") == "uzc"
    rx = re.compile(texts.button_regex("btn_cheks"))
    for lang in texts.LANGS:
        assert rx.match(texts.TEXTS[lang]["btn_cheks"])
    assert not rx.match("hello")
    for lang in texts.LANGS:
        assert set(texts.TEXTS[lang]) == set(texts.TEXTS["uz"]), lang


def test_format_orders_and_payments() -> None:
    unit_id = uuid.uuid4()

    class Ord:
        number = "UR-000001"
        payment = "paid"
        total = 1250000
        created_at = datetime(2026, 8, 16, 7, 5, tzinfo=UTC)  # 12:05 Tashkent

    Ord.unit_id = unit_id

    class P:
        amount = 500000
        created_at = datetime(2026, 8, 16, 7, 5, tzinfo=UTC)

    out = service.format_orders("uz", [(Ord(), ["Umumiy qon tahlili", "Glyukoza"])], {unit_id: "Bog‘ot SES"})
    assert out.startswith("🧾 Sizning cheklaringiz (oxirgi 1 ta):")
    assert "Chek № UR-000001 — 16.08.2026 12:05\n   🏥 Bog‘ot SES\n" in out
    assert "   • Umumiy qon tahlili\n   • Glyukoza\n" in out
    assert "Summa: 1\u00a0250\u00a0000 so'm" in out and "Holat: ✅ To'langan" in out  # money grouped with NBSP
    assert "🏥" not in service.format_orders("uz", [(Ord(), [])])  # no facility names given
    assert service.format_orders("en", []) == "You have no checks yet."
    pay = service.format_payments("ru", [(P(), "UR-000001")], 3, 900000)
    assert "✅ 16.08.2026 12:05 — 500\u00a0000 сум (Чек № UR-000001)" in pay
    assert pay.endswith("Итого: 3 платежей — 900\u00a0000 сум")
    assert service.clip("x" * 5000).endswith("\n...") and len(service.clip("x" * 5000)) == 4004
    assert service.result_caption("uz", "UR-1", "Qon tahlili") == "🧾 Chek № UR-1 — Qon tahlili"
    assert service.result_caption("uz", "UR-1", "Qon tahlili", "Bog‘ot SES") == "🧾 Chek № UR-1 — Qon tahlili · Bog‘ot SES"


def test_webhook_secret_and_bot_spec_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert service.webhook_secret_ok({"telegramWebhookSecret": "abc"}, "abc") and not service.webhook_secret_ok({"telegramWebhookSecret": "abc"}, "abd")
    assert not service.webhook_secret_ok({}, "") and not service.webhook_secret_ok(None, "abc")
    # the token: the account's own, else the platform bot's, else none
    own = MessagingAccount(unit_id=uuid.uuid4(), channel="telegram", provider="telegram", api_key_enc=encrypt("1:own"))
    platform = MessagingAccount(unit_id=uuid.uuid4(), channel="telegram", provider="telegram", api_key_enc=None)
    assert service.account_token(own) == "1:own" and service.account_token(None) is None
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    assert service.account_token(platform) is None
    monkeypatch.setattr(settings, "telegram_bot_token", "2:platform")
    assert service.account_token(platform) == "2:platform"
    assert service.default_lang_for({"locale": "ru"}) == "ru" and service.default_lang_for({"locale": "uzc"}) == "uz" and service.default_lang_for(None) == "uz"


# ----------------------------------------------------------------------------- link flow (DB)


async def _bot(s: Any, account_id: str) -> service.BotSpec:
    account = await tg_repo.get_account(s, uuid.UUID(account_id))
    assert account is not None
    return await service.bot_spec(s, account)


def test_bot_spec_resolves_the_account(ctx: dict) -> None:
    c: TestClient = ctx["client"]

    async def flow(s: Any) -> tuple[service.BotSpec, service.BotSpec]:
        return await _bot(s, ctx["bot_a"]), await _bot(s, ctx["bot_b"])

    bot_a, bot_b = db(c, flow)
    assert bot_a.unit_id == uuid.UUID(ctx["a"]["region"]) and bot_a.country_id == COUNTRY_ID and bot_a.token == TOKEN_A and bot_a.bot_username == "t_telegram_bot" and bot_a.webhook is False
    assert bot_b.token is None and bot_b.webhook is True and bot_b.default_lang == "uz"


def test_login_flow_links_patient(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a_fac = uuid.UUID(ctx["a"]["facility"])

    async def step1(s: Any) -> tuple[uuid.UUID, str]:
        bot_a = await _bot(s, ctx["bot_a"])
        r = await service.begin_login(s, bot_a, CHAT, "12", "uz")
        assert r.status == "bad_phone"
        r = await service.begin_login(s, bot_a, CHAT, "998900000000", "uz")
        assert r.status == "not_found"
        r = await service.begin_login(s, bot_a, CHAT, "+" + PHONE_A, "ru")
        assert r.status == "otp_sent" and r.challenge_id
        # the code went out through the patient's facility (which inherits the region's SMS key); the stored text is
        # masked, the real text travels encrypted in the payload
        sms = (await s.execute(select(OutboxMessage).where(OutboxMessage.unit_id == a_fac, OutboxMessage.kind == "otp"))).scalars().all()
        assert len(sms) == 1 and sms[0].text == "Sizning tasdiqlash kodingiz: ****" and sms[0].to == PHONE_A and sms[0].status == "queued"
        code = messaging.outgoing_text(sms[0]).rsplit(" ", 1)[-1]
        assert len(code) == settings.otp_length
        # a second code right away is refused (cooldown)
        assert (await service.begin_login(s, bot_a, CHAT, PHONE_A, "ru")).status == "too_many"
        return r.challenge_id, code

    challenge_id, code = db(c, step1)

    async def step2(s: Any) -> None:
        bot_a = await _bot(s, ctx["bot_a"])
        bot_b = await _bot(s, ctx["bot_b"])
        # wrong code → attempts left decreases
        v = await service.verify_code(s, bot_a, CHAT, challenge_id, "0000" if code != "0000" else "0001", "ru")
        assert v.status == "wrong" and v.attempts_left == settings.otp_max_attempts - 1
        # another chat or another bot cannot use this challenge
        assert (await service.verify_code(s, bot_a, "other-chat", challenge_id, code, "ru")).status == "expired"
        assert (await service.verify_code(s, bot_b, CHAT, challenge_id, code, "ru")).status == "expired"
        v = await service.verify_code(s, bot_a, CHAT, challenge_id, code, "ru")
        assert v.status == "linked" and v.patient_name == "T-telegram Bemor A" and v.org_name == f"T-{ctx['tag']} tumani"

    db(c, step2)

    async def step3(s: Any) -> None:
        bot_a = await _bot(s, ctx["bot_a"])
        p = await s.get(Patient, uuid.UUID(ctx["pat_a"]))
        assert p.telegram_chat_id == CHAT and p.portal_linked is True
        link = (await s.execute(select(TelegramLink).where(TelegramLink.chat_id == CHAT, TelegramLink.account_id == uuid.UUID(ctx["bot_a"])))).scalar_one()
        assert link.patient_id == p.id and link.lang == "ru" and link.unlinked_at is None
        ch = await s.get(OtpChallenge, challenge_id)
        assert ch.consumed_at is not None and ch.attempts == 1 and ch.unit_id == a_fac
        # views
        assert await service.orders_text(s, p.id, "uz") == "Sizda hozircha cheklar yo'q."
        assert await service.payments_text(s, p.id, "en") == "You have no payments yet."
        assert await service.result_files(s, p.id, "uz") == []
        assert await service.patient_org_name(s, p) == f"T-{ctx['tag']} tumani"
        # logout
        assert await service.logout(s, bot_a, "nobody-" + CHAT, "uz") is None
        assert await service.logout(s, bot_a, CHAT, "uz") == "T-telegram Bemor A"

    db(c, step3)

    async def step4(s: Any) -> None:
        bot_a = await _bot(s, ctx["bot_a"])
        p = await s.get(Patient, uuid.UUID(ctx["pat_a"]))
        assert p.telegram_chat_id is None
        link = (await s.execute(select(TelegramLink).where(TelegramLink.chat_id == CHAT, TelegramLink.account_id == uuid.UUID(ctx["bot_a"])))).scalar_one()
        assert link.unlinked_at is not None
        assert await service.logout(s, bot_a, CHAT, "uz") is None
        # expired / exhausted challenge
        meta = {"chatId": CHAT, "accountId": ctx["bot_a"], "patientId": ctx["pat_a"]}
        old = OtpChallenge(phone=PHONE_A, purpose="telegram", code_hash=sha256_hex("1111"), max_attempts=1, expires_at=datetime.now(UTC) - timedelta(seconds=1), unit_id=a_fac, meta=meta)
        s.add(old)
        await s.flush()
        assert (await service.verify_code(s, bot_a, CHAT, old.id, "1111", "uz")).status == "expired"
        fresh = OtpChallenge(phone=PHONE_A, purpose="telegram", code_hash=sha256_hex("1111"), max_attempts=1, expires_at=datetime.now(UTC) + timedelta(minutes=5), unit_id=a_fac, meta=meta)
        s.add(fresh)
        await s.flush()
        assert (await service.verify_code(s, bot_a, CHAT, fresh.id, "2222", "uz")).status == "over"
        assert (await service.verify_code(s, bot_a, CHAT, fresh.id, "1111", "uz")).status == "expired"

    db(c, step4)


def test_chat_lang_roundtrip(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    account = uuid.UUID(ctx["bot_a"])

    async def flow(s: Any) -> None:
        assert await service.chat_lang(s, account, CHAT + "x") == "uz"
        assert await service.set_chat_lang(s, account, CHAT + "x", "en") == "en"
        assert await service.set_chat_lang(s, account, CHAT + "x", "??") == "uz"
        assert await service.set_chat_lang(s, account, CHAT + "x", "uzc") == "uzc"

    db(c, flow)

    async def again(s: Any) -> None:
        assert await service.chat_lang(s, account, CHAT + "x") == "uzc"
        # another bot's chat preferences are its own
        assert await service.chat_lang(s, uuid.UUID(ctx["bot_b"]), CHAT + "x") == "uz"

    db(c, again)


# ----------------------------------------------------------------------------- deliver() with a fake bot


class FakeMessage:
    message_id = 4242


class FakeBot:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.opened_with: list[tuple[uuid.UUID, str]] = []
        self.fail = fail

    async def send_message(self, **kw: Any) -> FakeMessage:
        self.calls.append(("send_message", kw))
        if self.fail:
            raise self.fail
        return FakeMessage()

    async def send_document(self, **kw: Any) -> FakeMessage:
        self.calls.append(("send_document", kw))
        if self.fail:
            raise self.fail
        return FakeMessage()


def _fake_open_bot(bot: FakeBot | None):
    @contextlib.asynccontextmanager
    async def _open(account_id: uuid.UUID, token: str):
        if bot is not None:
            bot.opened_with.append((account_id, token))
        yield bot

    return _open


def _msg(unit_id: str, **kw: Any) -> OutboxMessage:
    base = {"unit_id": uuid.UUID(unit_id), "channel": "telegram", "kind": "payment_receipt", "to": CHAT, "text": "T-telegram hello", "status": "sending", "payload": {}}
    base.update(kw)
    return OutboxMessage(**base)


def test_deliver_text_and_errors(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    from telegram.error import Forbidden, TimedOut

    async def flow(s: Any) -> None:
        bot = FakeBot()
        monkeypatch.setattr(bot_manager, "_open_bot", _fake_open_bot(bot))
        # a message of facility A goes out through the region's bot (the account is recorded on the row)
        m = _msg(ctx["a"]["facility"])
        s.add(m)
        await s.flush()
        assert await bot_manager.deliver(s, m) is True
        assert bot.calls == [("send_message", {"chat_id": CHAT, "text": "T-telegram hello"})]
        assert bot.opened_with == [(uuid.UUID(ctx["bot_a"]), TOKEN_A)]
        assert m.provider_message_id == "4242" and m.account_id == uuid.UUID(ctx["bot_a"])
        # tree B's bot has no token of its own and the platform bot is not configured → not configured, no send
        monkeypatch.setattr(settings, "telegram_bot_token", "")
        m2 = _msg(ctx["b"]["facility"])
        s.add(m2)
        await s.flush()
        assert await bot_manager.deliver(s, m2) is False and m2.error == "telegram_not_configured"
        assert len(bot.calls) == 1
        # permanent error (blocked) → False with error
        monkeypatch.setattr(bot_manager, "_open_bot", _fake_open_bot(FakeBot(fail=Forbidden("bot was blocked by the user"))))
        m3 = _msg(ctx["a"]["facility"])
        s.add(m3)
        await s.flush()
        assert await bot_manager.deliver(s, m3) is False and m3.error.startswith("telegram_forbidden")
        # transient error → raises (dispatcher retries)
        monkeypatch.setattr(bot_manager, "_open_bot", _fake_open_bot(FakeBot(fail=TimedOut())))
        m4 = _msg(ctx["a"]["facility"])
        s.add(m4)
        await s.flush()
        with pytest.raises(TimedOut):
            await bot_manager.deliver(s, m4)

    db(c, flow)


def test_deliver_result_pdf(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.infrastructure.db.models import Order, ResultDocument

    c: TestClient = ctx["client"]
    pat = uuid.UUID(ctx["pat_a"])
    fac = uuid.UUID(ctx["a"]["facility"])

    async def fake_pdf(session: Any, document: ResultDocument) -> bytes:
        return b"%PDF-1.4 T-telegram " + str(document.id).encode()

    async def flow(s: Any) -> None:
        order = Order(unit_id=fac, number=f"{ctx['code_a']}-000077", patient_id=pat, patient_name="T-telegram Bemor A", patient_phone=PHONE_A, created_by_employee_id=uuid.UUID(ctx["emp_a"]), status="completed", payment="paid")
        s.add(order)
        await s.flush()
        doc = ResultDocument(unit_id=fac, order_id=order.id, patient_id=pat, template_id=uuid.UUID(ctx["tpl"]), title="Qon tahlili", status="final", snapshot={})
        s.add(doc)
        await s.flush()
        bot = FakeBot()
        monkeypatch.setattr(bot_manager, "_open_bot", _fake_open_bot(bot))
        monkeypatch.setattr(service, "document_pdf", fake_pdf)
        m = _msg(ctx["a"]["facility"], kind="result_ready", text="Natija tayyor", document_id=doc.id, patient_id=pat, payload={"documentId": str(doc.id)})
        s.add(m)
        await s.flush()
        assert await bot_manager.deliver(s, m) is True
        name, kw = bot.calls[0]
        assert name == "send_document" and kw["chat_id"] == CHAT and kw["caption"] == "Natija tayyor" and kw["filename"] == "qon-taxlili.pdf"
        assert kw["document"].startswith(b"%PDF-1.4 T-telegram ")
        # the bot's "results" view finds the same document, captioned with the facility
        files = await service.result_files(s, pat, "uz")
        assert len(files) == 1 and files[0].caption == f"🧾 Chek № {ctx['code_a']}-000077 — Qon tahlili · T-{ctx['tag']} SES" and files[0].filename == "qon-taxlili.pdf"
        # an unknown document (or one of another patient) is not sent — falls back to text
        m2 = _msg(ctx["a"]["facility"], kind="result_ready", text="x", payload={"documentId": str(uuid.uuid4())})
        s.add(m2)
        await s.flush()
        assert await bot_manager.deliver(s, m2) is True and bot.calls[-1][0] == "send_message"
        text = await service.orders_text(s, pat, "uz")
        assert text.startswith(f"🧾 Sizning cheklaringiz (oxirgi 1 ta):\n\nChek № {ctx['code_a']}-000077 — ") and f"🏥 T-{ctx['tag']} SES" in text

    db(c, flow)


# ----------------------------------------------------------------------------- HTTP


def test_status_endpoint(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["a"], ctx["b"]
    r = c.get(f"{API}/units/{a['facility']}/telegram/status", headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json() == {"connected": True, "botUsername": "t_telegram_bot", "running": False, "accountId": ctx["bot_a"], "inherited": True, "effectiveUnitId": a["region"]}
    assert c.get(f"{API}/units/{a['region']}/telegram/status", headers=h(ctx, "admin-a")).json()["inherited"] is False
    r = c.get(f"{API}/units/{b['facility']}/telegram/status", headers=h(ctx, "admin-b"))
    assert r.json()["connected"] is True and r.json()["botUsername"] is None and r.json()["accountId"] == ctx["bot_b"]  # the platform bot
    assert c.get(f"{API}/units/{b['facility']}/telegram/status", headers=h(ctx, "admin-a")).status_code == 403
    assert c.get(f"{API}/units/{a['facility']}/telegram/status").status_code == 401


def test_webhook_endpoint(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    update = {"update_id": 1, "message": {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"}, "text": "/start"}}
    assert c.post(f"{API}/telegram/webhook/{ctx['bot_a']}/whatever", json=update).status_code == 404  # no secret configured
    assert c.post(f"{API}/telegram/webhook/{ctx['bot_b']}/wrong", json=update).status_code == 404
    assert c.post(f"{API}/telegram/webhook/{uuid.uuid4()}/whsec-{SFX}", json=update).status_code == 404
    r = c.post(f"{API}/telegram/webhook/{ctx['bot_b']}/whsec-{SFX}", json=update)
    assert r.status_code == 200 and r.json() == {"ok": True}  # accepted; bot not running here → dropped


def test_manager_stop_without_bots() -> None:
    """A manager with no bots reports nothing running and stops cleanly."""

    async def flow() -> None:
        from app.modules.telegram.manager import BotManager

        m = BotManager()
        assert m.is_running(uuid.uuid4()) is False and m.application_for(uuid.uuid4()) is None
        await m.stop()

    asyncio.run(flow())
