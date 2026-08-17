"""Telegram module tests: pure helpers, link flow + delivery against the real dev DB (rows prefixed T-telegram-),
status/webhook endpoints. No live Telegram calls — the bot is a fake object."""


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
from app.core.permissions import COMPANY_ADMIN_PERMISSIONS
from app.core.security import hash_password, sha256_hex
from app.infrastructure.db.models import Company, Employee, OtpChallenge, OutboxMessage, Patient, Role, TelegramLink
from app.infrastructure.db.session import engine, session_scope
from app.modules.messaging import service as messaging
from app.modules.telegram import service, texts
from app.modules.telegram.manager import bot_manager
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

settings.workers_enabled = False
settings.telegram_enabled = False

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-telegram-pass-1"
PHONE_A = "9989" + f"{int(SFX, 16) % 10**8:08d}"  # 12 digits, unique per run
CHAT = str(10**9 + int(SFX, 16) % 10**8)


async def _seed() -> dict[str, str]:
    async with session_scope() as s:
        a = Company(name=f"T-telegram-A-{SFX}", slug=f"t-telegram-a-{SFX}", telegram_bot_token_enc=encrypt("123456:T-telegram-token"), telegram_bot_username="t_telegram_bot", sms_provider="xabarchi", sms_api_key_enc=encrypt("t-telegram-sms-key"), sms_api_key_masked="t-te••••-key")
        b = Company(name=f"T-telegram-B-{SFX}", slug=f"t-telegram-b-{SFX}", settings={"telegramWebhookSecret": f"whsec-{SFX}"})
        s.add_all([a, b])
        await s.flush()
        role_a = Role(company_id=a.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        role_b = Role(company_id=b.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        s.add_all([role_a, role_b])
        await s.flush()
        pw = hash_password(PASSWORD)
        emp_a = Employee(company_id=a.id, full_name="T-telegram Admin A", login=f"t-telegram-admin-a-{SFX}", password_hash=pw, role_id=role_a.id)
        emp_b = Employee(company_id=b.id, full_name="T-telegram Admin B", login=f"t-telegram-admin-b-{SFX}", password_hash=pw, role_id=role_b.id)
        pat_a = Patient(company_id=a.id, full_name="T-telegram Bemor A", phone=PHONE_A, passport_number=f"TG{SFX}A")
        s.add_all([emp_a, emp_b, pat_a])
        await s.flush()
        ids = {"a": str(a.id), "b": str(b.id), "pat_a": str(pat_a.id)}
    await engine.dispose()
    return ids


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict[str, object]]:
    ids = asyncio.run(_seed())
    from app.main import app

    with TestClient(app) as client:
        tokens = {}
        for who in ("admin-a", "admin-b"):
            r = client.post("/api/v1/auth/staff/login", json={"login": f"t-telegram-{who}-{SFX}", "password": PASSWORD})
            assert r.status_code == 200, r.text
            tokens[who] = r.json()["accessToken"]
        yield {"client": client, "ids": ids, "tokens": tokens}


def h(ctx: dict, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ctx['tokens'][who]}"}


Scope = async_sessionmaker[AsyncSession]


def run(fn: Any) -> None:
    """Run `fn(sessionmaker)` on a fresh loop with its own engine (the shared engine belongs to the TestClient loop)."""

    async def _wrapped() -> None:
        own = create_async_engine(settings.sqlalchemy_url)
        sm: Scope = async_sessionmaker(own, expire_on_commit=False, autoflush=False)
        try:
            await fn(sm)
        finally:
            await own.dispose()

    asyncio.run(_wrapped())


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
    class Ord:
        number = "UR-000001"
        payment = "paid"
        total = 1250000
        created_at = datetime(2026, 8, 16, 7, 5, tzinfo=UTC)  # 12:05 Tashkent

    class P:
        amount = 500000
        created_at = datetime(2026, 8, 16, 7, 5, tzinfo=UTC)

    out = service.format_orders("uz", [(Ord(), ["Umumiy qon tahlili", "Glyukoza"])])
    assert out.startswith("🧾 Sizning cheklaringiz (oxirgi 1 ta):")
    assert "Chek № UR-000001 — 16.08.2026 12:05" in out
    assert "   • Umumiy qon tahlili\n   • Glyukoza\n" in out
    assert "Summa: 1 250 000 so'm" in out and "Holat: ✅ To'langan" in out
    assert service.format_orders("en", []) == "You have no checks yet."
    pay = service.format_payments("ru", [(P(), "UR-000001")], 3, 900000)
    assert "✅ 16.08.2026 12:05 — 500 000 сум (Чек № UR-000001)" in pay
    assert pay.endswith("Итого: 3 платежей — 900 000 сум")
    assert service.clip("x" * 5000).endswith("\n...") and len(service.clip("x" * 5000)) == 4004
    assert service.result_caption("uz", "UR-1", "Qon tahlili") == "🧾 Chek № UR-1 — Qon tahlili"


def test_webhook_secret_check() -> None:
    class C:
        settings = {"telegramWebhookSecret": "abc"}

    class N:
        settings = {}

    assert service.webhook_secret_ok(C(), "abc") and not service.webhook_secret_ok(C(), "abd")
    assert not service.webhook_secret_ok(N(), "") and not service.webhook_secret_ok(None, "abc")


# ----------------------------------------------------------------------------- link flow (DB)


def test_login_flow_links_patient_and_isolates_tenants(ctx: dict) -> None:
    a = uuid.UUID(ctx["ids"]["a"])
    b = uuid.UUID(ctx["ids"]["b"])

    async def flow(sm: Scope) -> None:
        async with sm.begin() as s:
            company_b = await s.get(Company, b)
            # patient of company A is invisible to company B's bot
            r = await service.begin_login(s, company_b, CHAT, PHONE_A, "uz")
            assert r.status == "not_found"
            r = await service.begin_login(s, company_b, CHAT, "12", "uz")
            assert r.status == "bad_phone"
            company_a = await s.get(Company, a)
            r = await service.begin_login(s, company_a, CHAT, "+" + PHONE_A, "ru")
            assert r.status == "otp_sent" and r.challenge_id
            challenge_id = r.challenge_id
            sms = (await s.execute(select(OutboxMessage).where(OutboxMessage.company_id == a, OutboxMessage.kind == "otp"))).scalars().all()
            # the stored text is masked; the real text (with the code) travels encrypted in the payload
            assert len(sms) == 1 and sms[0].text == "Ваш код подтверждения: ****" and sms[0].to == PHONE_A
            code = messaging.outgoing_text(sms[0]).rsplit(" ", 1)[-1]
            assert len(code) == settings.otp_length
            # wrong code twice → attempts left decreases
            v = await service.verify_code(s, a, CHAT, challenge_id, "0000" if code != "0000" else "0001", "ru")
            assert v.status == "wrong" and v.attempts_left == settings.otp_max_attempts - 1
            # another chat cannot use this challenge
            v = await service.verify_code(s, a, "other-chat", challenge_id, code, "ru")
            assert v.status == "expired"
            v = await service.verify_code(s, a, CHAT, challenge_id, code, "ru")
            assert v.status == "linked" and v.patient_name == "T-telegram Bemor A"
        async with sm.begin() as s:
            p = await s.get(Patient, uuid.UUID(ctx["ids"]["pat_a"]))
            assert p.telegram_chat_id == CHAT and p.portal_linked is True
            link = (await s.execute(select(TelegramLink).where(TelegramLink.chat_id == CHAT, TelegramLink.company_id == a))).scalar_one()
            assert link.patient_id == p.id and link.lang == "ru" and link.unlinked_at is None
            ch = await s.get(OtpChallenge, challenge_id)
            assert ch.consumed_at is not None and ch.attempts == 1
            # views
            assert await service.orders_text(s, a, p.id, "uz") == "Sizda hozircha cheklar yo'q."
            assert await service.payments_text(s, a, p.id, "en") == "You have no payments yet."
            assert await service.result_files(s, a, p.id, "uz") == []
            # logout
            assert await service.logout(s, b, CHAT, "uz") is None  # other tenant: nothing linked
            assert await service.logout(s, a, CHAT, "uz") == "T-telegram Bemor A"
        async with sm.begin() as s:
            p = await s.get(Patient, uuid.UUID(ctx["ids"]["pat_a"]))
            assert p.telegram_chat_id is None
            link = (await s.execute(select(TelegramLink).where(TelegramLink.chat_id == CHAT, TelegramLink.company_id == a))).scalar_one()
            assert link.unlinked_at is not None
            assert await service.logout(s, a, CHAT, "uz") is None
            # expired / exhausted challenge
            old = OtpChallenge(phone=PHONE_A, purpose="telegram", code_hash=sha256_hex("1111"), max_attempts=1, expires_at=datetime.now(UTC) - timedelta(seconds=1), company_id=a, meta={"chatId": CHAT})
            s.add(old)
            await s.flush()
            assert (await service.verify_code(s, a, CHAT, old.id, "1111", "uz")).status == "expired"
            fresh = OtpChallenge(phone=PHONE_A, purpose="telegram", code_hash=sha256_hex("1111"), max_attempts=1, expires_at=datetime.now(UTC) + timedelta(minutes=5), company_id=a, meta={"chatId": CHAT})
            s.add(fresh)
            await s.flush()
            assert (await service.verify_code(s, a, CHAT, fresh.id, "2222", "uz")).status == "over"
            assert (await service.verify_code(s, a, CHAT, fresh.id, "1111", "uz")).status == "expired"

    run(flow)


def test_chat_lang_roundtrip(ctx: dict) -> None:
    a = uuid.UUID(ctx["ids"]["a"])

    async def flow(sm: Scope) -> None:
        async with sm.begin() as s:
            assert await service.chat_lang(s, a, CHAT + "x") == "uz"
            assert await service.set_chat_lang(s, a, CHAT + "x", "en") == "en"
            assert await service.set_chat_lang(s, a, CHAT + "x", "??") == "uz"
            assert await service.set_chat_lang(s, a, CHAT + "x", "uzc") == "uzc"
        async with sm.begin() as s:
            assert await service.chat_lang(s, a, CHAT + "x") == "uzc"

    run(flow)


# ----------------------------------------------------------------------------- deliver() with a fake bot


class FakeMessage:
    message_id = 4242


class FakeBot:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
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
    async def _open(company: Company):
        yield bot

    return _open


def _msg(company_id: uuid.UUID, **kw: Any) -> OutboxMessage:
    base = {"company_id": company_id, "channel": "telegram", "kind": "payment_receipt", "to": CHAT, "text": "T-telegram hello", "status": "sending", "payload": {}}
    base.update(kw)
    return OutboxMessage(**base)


def test_deliver_text_and_errors(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    a = uuid.UUID(ctx["ids"]["a"])
    b = uuid.UUID(ctx["ids"]["b"])
    from telegram.error import Forbidden, TimedOut

    async def flow(sm: Scope) -> None:
        async with sm.begin() as s:
            bot = FakeBot()
            monkeypatch.setattr(bot_manager, "_open_bot", _fake_open_bot(bot))
            m = _msg(a)
            s.add(m)
            await s.flush()
            assert await bot_manager.deliver(s, m) is True
            assert bot.calls == [("send_message", {"chat_id": CHAT, "text": "T-telegram hello"})]
            assert m.provider_message_id == "4242"
            # company without bot → not configured, no send
            m2 = _msg(b)
            s.add(m2)
            await s.flush()
            assert await bot_manager.deliver(s, m2) is False and m2.error == "telegram_not_configured"
            assert len(bot.calls) == 1
            # permanent error (blocked) → False with error
            monkeypatch.setattr(bot_manager, "_open_bot", _fake_open_bot(FakeBot(fail=Forbidden("bot was blocked by the user"))))
            m3 = _msg(a)
            s.add(m3)
            await s.flush()
            assert await bot_manager.deliver(s, m3) is False and m3.error.startswith("telegram_forbidden")
            # transient error → raises (dispatcher retries)
            monkeypatch.setattr(bot_manager, "_open_bot", _fake_open_bot(FakeBot(fail=TimedOut())))
            m4 = _msg(a)
            s.add(m4)
            await s.flush()
            with pytest.raises(TimedOut):
                await bot_manager.deliver(s, m4)

    run(flow)


def test_deliver_result_pdf(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.infrastructure.db.models import Order, ResultDocument

    a = uuid.UUID(ctx["ids"]["a"])
    pat = uuid.UUID(ctx["ids"]["pat_a"])

    async def fake_pdf(session: Any, document: ResultDocument) -> bytes:
        return b"%PDF-1.4 T-telegram " + str(document.id).encode()

    async def flow(sm: Scope) -> None:
        async with sm.begin() as s:
            order = Order(company_id=a, branch_id=uuid.uuid4(), number=f"TG-{SFX}", patient_id=pat, patient_name="T-telegram Bemor A", patient_phone=PHONE_A, created_by_employee_id=uuid.uuid4(), status="completed", payment="paid")
            s.add(order)
            await s.flush()
            doc = ResultDocument(company_id=a, order_id=order.id, patient_id=pat, template_id=uuid.uuid4(), title="Qon tahlili", status="final", snapshot={})
            s.add(doc)
            await s.flush()
            bot = FakeBot()
            monkeypatch.setattr(bot_manager, "_open_bot", _fake_open_bot(bot))
            monkeypatch.setattr(service, "document_pdf", fake_pdf)
            m = _msg(a, kind="result_ready", text="Natija tayyor", document_id=doc.id, payload={"documentId": str(doc.id)})
            s.add(m)
            await s.flush()
            assert await bot_manager.deliver(s, m) is True
            name, kw = bot.calls[0]
            assert name == "send_document" and kw["chat_id"] == CHAT and kw["caption"] == "Natija tayyor" and kw["filename"] == "qon-taxlili.pdf"
            assert kw["document"].startswith(b"%PDF-1.4 T-telegram ")
            # the bot's "results" view finds the same document
            files = await service.result_files(s, a, pat, "uz")
            assert len(files) == 1 and files[0].caption == f"🧾 Chek № TG-{SFX} — Qon tahlili" and files[0].filename == "qon-taxlili.pdf"
            # document of another company is not sent — falls back to text
            m2 = _msg(a, kind="result_ready", text="x", payload={"documentId": str(uuid.uuid4())})
            s.add(m2)
            await s.flush()
            assert await bot_manager.deliver(s, m2) is True and bot.calls[-1][0] == "send_message"
            assert (await service.orders_text(s, a, pat, "uz")).startswith(f"🧾 Sizning cheklaringiz (oxirgi 1 ta):\n\nChek № TG-{SFX} — ")

    run(flow)


# ----------------------------------------------------------------------------- HTTP


def test_status_endpoint(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["ids"]["a"], ctx["ids"]["b"]
    r = c.get(f"/api/v1/companies/{a}/telegram/status", headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json() == {"connected": True, "botUsername": "t_telegram_bot", "running": False}
    r = c.get(f"/api/v1/companies/{b}/telegram/status", headers=h(ctx, "admin-b"))
    assert r.json() == {"connected": False, "botUsername": None, "running": False}
    assert c.get(f"/api/v1/companies/{b}/telegram/status", headers=h(ctx, "admin-a")).status_code == 403
    assert c.get(f"/api/v1/companies/{a}/telegram/status").status_code == 401


def test_webhook_endpoint(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["ids"]["a"], ctx["ids"]["b"]
    update = {"update_id": 1, "message": {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"}, "text": "/start"}}
    assert c.post(f"/api/v1/telegram/webhook/{a}/whatever", json=update).status_code == 404  # no secret configured
    assert c.post(f"/api/v1/telegram/webhook/{b}/wrong", json=update).status_code == 404
    assert c.post(f"/api/v1/telegram/webhook/{uuid.uuid4()}/whsec-{SFX}", json=update).status_code == 404
    r = c.post(f"/api/v1/telegram/webhook/{b}/whsec-{SFX}", json=update)
    assert r.status_code == 200 and r.json() == {"ok": True}  # accepted; bot not running here → dropped


def test_manager_stop_without_bots() -> None:
    """A manager with no bots reports nothing running and stops cleanly."""

    async def flow(sm: Scope) -> None:
        from app.modules.telegram.manager import BotManager

        m = BotManager()
        assert m.is_running(uuid.uuid4()) is False and m.application_for(uuid.uuid4()) is None
        await m.stop()

    run(flow)
