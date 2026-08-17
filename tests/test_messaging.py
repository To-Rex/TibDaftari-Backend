"""API + worker tests for the messaging module against the real dev DB (fixture rows prefixed T-messaging-)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import settings
from app.core.crypto import encrypt
from app.core.permissions import COMPANY_ADMIN_PERMISSIONS
from app.core.security import hash_password
from app.infrastructure.db.models import Company, Employee, Notification, OutboxMessage, Role
from app.infrastructure.db.session import engine, session_scope
from app.modules.messaging import dispatcher, maintenance, xabarchi
from app.modules.messaging.xabarchi import ProviderResult, XabarchiTransientError
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

settings.workers_enabled = False
settings.telegram_enabled = False

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-messaging-pass-1"


async def _seed() -> dict[str, str]:
    async with session_scope() as s:
        a = Company(name=f"T-messaging-A-{SFX}", slug=f"t-messaging-a-{SFX}", sms_provider="xabarchi", sms_api_key_enc=encrypt("xab_test_key"), sms_api_key_masked="xab_••••")
        b = Company(name=f"T-messaging-B-{SFX}", slug=f"t-messaging-b-{SFX}")
        s.add_all([a, b])
        await s.flush()
        admin_a = Role(company_id=a.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        admin_b = Role(company_id=b.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        sender_a = Role(company_id=a.id, key="sender", name="Sender", permissions=["messaging.send"], is_system=False)
        s.add_all([admin_a, admin_b, sender_a])
        await s.flush()
        pw = hash_password(PASSWORD)
        emp_admin_a = Employee(company_id=a.id, full_name="T-messaging Admin A", login=f"t-messaging-admin-a-{SFX}", password_hash=pw, role_id=admin_a.id)
        emp_admin_b = Employee(company_id=b.id, full_name="T-messaging Admin B", login=f"t-messaging-admin-b-{SFX}", password_hash=pw, role_id=admin_b.id)
        emp_sender_a = Employee(company_id=a.id, full_name="T-messaging Sender A", login=f"t-messaging-sender-a-{SFX}", password_hash=pw, role_id=sender_a.id)
        s.add_all([emp_admin_a, emp_admin_b, emp_sender_a])
        await s.flush()
        s.add_all(
            [
                Notification(company_id=a.id, title="T-messaging company-wide", body="for everyone", kind="info", read_by=[]),
                Notification(company_id=a.id, employee_id=emp_admin_a.id, title="T-messaging personal", body="admin only", kind="warning", read_by=[]),
                Notification(company_id=a.id, employee_id=emp_sender_a.id, title="T-messaging other person", body="sender only", kind="success", read_by=[]),
                Notification(company_id=b.id, title="T-messaging company B", body="other tenant", kind="info", read_by=[]),
            ]
        )
        ids = {"a": str(a.id), "b": str(b.id), "admin_a": str(emp_admin_a.id), "sender_a": str(emp_sender_a.id)}
    await engine.dispose()
    return ids


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict[str, object]]:
    ids = asyncio.run(_seed())
    with TestClient(app_instance()) as client:
        tokens = {}
        for who in ("admin-a", "admin-b", "sender-a"):
            r = client.post("/api/v1/auth/staff/login", json={"login": f"t-messaging-{who}-{SFX}", "password": PASSWORD})
            assert r.status_code == 200, r.text
            tokens[who] = r.json()["accessToken"]
        yield {"client": client, "ids": ids, "tokens": tokens}


def app_instance():
    from app.main import app

    return app


def h(ctx: dict, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ctx['tokens'][who]}"}


OUTBOX_KEYS = {
    "id", "companyId", "branchId", "patientId", "orderId", "channel", "kind", "to", "text", "status",
    "scheduledAt", "sentAt", "attempts", "providerMessageId", "error", "createdAt", "updatedAt",
}  # fmt: skip


def test_send_and_list_outbox(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    # broadcast (2 distinct + 1 duplicate + 1 invalid) by admin
    r = c.post(
        f"/api/v1/companies/{cid}/messages/send",
        json={"to": ["+998 90 123-45-67", "901234567", "998911112233", "12345"], "text": f"T-messaging salom {SFX}", "kind": "broadcast"},
        headers=h(ctx, "admin-a"),
    )
    assert r.status_code == 201, r.text
    msgs = r.json()
    assert len(msgs) == 2 and {m["to"] for m in msgs} == {"998901234567", "998911112233"}
    assert set(msgs[0]) == OUTBOX_KEYS
    assert msgs[0]["status"] == "queued" and msgs[0]["channel"] == "sms" and msgs[0]["attempts"] == 0 and msgs[0]["companyId"] == cid
    assert r.headers.get("X-Invalid-Recipients") == "12345"
    # scheduled in the future
    future = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = c.post(f"/api/v1/companies/{cid}/messages/send", json={"to": ["998900000001"], "text": "T-messaging later", "kind": "reminder", "scheduledAt": future}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201 and r.json()[0]["status"] == "scheduled" and r.json()[0]["scheduledAt"] == future
    # sender without messaging.broadcast: 1 recipient ok, 2 → 403
    assert c.post(f"/api/v1/companies/{cid}/messages/send", json={"to": ["998900000002"], "text": "T-messaging one", "kind": "broadcast"}, headers=h(ctx, "sender-a")).status_code == 201
    r = c.post(f"/api/v1/companies/{cid}/messages/send", json={"to": ["998900000002", "998900000003"], "text": "T-messaging two", "kind": "broadcast"}, headers=h(ctx, "sender-a"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    # all invalid → 422 invalid_phone; empty text → 422
    r = c.post(f"/api/v1/companies/{cid}/messages/send", json={"to": ["abc"], "text": "x", "kind": "broadcast"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_phone"
    assert c.post(f"/api/v1/companies/{cid}/messages/send", json={"to": ["998900000002"], "text": "", "kind": "broadcast"}, headers=h(ctx, "admin-a")).status_code == 422
    # tenant isolation
    assert c.post(f"/api/v1/companies/{cid}/messages/send", json={"to": ["998900000002"], "text": "x", "kind": "broadcast"}, headers=h(ctx, "admin-b")).status_code == 403
    assert c.get(f"/api/v1/companies/{cid}/outbox", headers=h(ctx, "admin-b")).status_code == 403
    # list: newest first, filters, search by digits and by folded text
    r = c.get(f"/api/v1/companies/{cid}/outbox", headers=h(ctx, "admin-a"))
    assert r.status_code == 200
    page = r.json()
    assert set(page) == {"items", "page", "pageSize", "total", "totalPages"} and page["total"] >= 4
    created = [m["createdAt"] for m in page["items"]]
    assert created == sorted(created, reverse=True)
    r = c.get(f"/api/v1/companies/{cid}/outbox", params={"status": "scheduled", "kind": "reminder"}, headers=h(ctx, "admin-a"))
    assert r.json()["total"] == 1 and r.json()["items"][0]["to"] == "998900000001"
    r = c.get(f"/api/v1/companies/{cid}/outbox", params={"search": "91 111"}, headers=h(ctx, "admin-a"))
    assert {m["to"] for m in r.json()["items"]} == {"998911112233"}
    r = c.get(f"/api/v1/companies/{cid}/outbox", params={"search": f"SALOM {SFX}"}, headers=h(ctx, "admin-a"))
    assert r.json()["total"] == 2
    assert c.get(f"/api/v1/companies/{cid}/outbox", params={"status": "bogus"}, headers=h(ctx, "admin-a")).status_code == 422
    # sender may list (messaging.send)
    assert c.get(f"/api/v1/companies/{cid}/outbox", headers=h(ctx, "sender-a")).status_code == 200


def test_notifications_and_mark_read(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    r = c.get("/api/v1/notifications", headers=h(ctx, "admin-a"))
    assert r.status_code == 200
    items = [n for n in r.json() if n["title"].startswith("T-messaging")]
    assert {n["title"] for n in items} == {"T-messaging company-wide", "T-messaging personal"}
    assert set(items[0]) == {"id", "title", "body", "kind", "createdAt", "read", "link"}
    assert all(n["read"] is False for n in items)
    personal = next(n for n in items if n["title"] == "T-messaging personal")
    assert c.post("/api/v1/notifications/read", json={"id": personal["id"]}, headers=h(ctx, "admin-a")).status_code == 200
    items = {n["title"]: n for n in c.get("/api/v1/notifications", headers=h(ctx, "admin-a")).json() if n["title"].startswith("T-messaging")}
    assert items["T-messaging personal"]["read"] is True and items["T-messaging company-wide"]["read"] is False
    # sender: company-wide still unread for them (read_by is per employee)
    sender_items = {n["title"]: n for n in c.get("/api/v1/notifications", headers=h(ctx, "sender-a")).json() if n["title"].startswith("T-messaging")}
    assert set(sender_items) == {"T-messaging company-wide", "T-messaging other person"} and sender_items["T-messaging company-wide"]["read"] is False
    # mark all
    assert c.post("/api/v1/notifications/read", json={}, headers=h(ctx, "admin-a")).status_code == 200
    items = [n for n in c.get("/api/v1/notifications", headers=h(ctx, "admin-a")).json() if n["title"].startswith("T-messaging")]
    assert all(n["read"] for n in items)
    # tenant B never sees A's notifications
    titles_b = {n["title"] for n in c.get("/api/v1/notifications", headers=h(ctx, "admin-b")).json()}
    assert "T-messaging company B" in titles_b and not (titles_b & {"T-messaging company-wide", "T-messaging personal"})


# ----------------------------------------------------------------------------- dispatcher (worker) tests


async def _insert_outbox(company_id: str, to: str, *, status: str = "queued", next_attempt: datetime | None = None) -> uuid.UUID:
    own = create_async_engine(settings.sqlalchemy_url)
    async with AsyncSession(own, expire_on_commit=False) as s, s.begin():
        m = OutboxMessage(company_id=uuid.UUID(company_id), channel="sms", kind="broadcast", to=to, text="T-messaging dispatch", status=status, next_attempt_at=next_attempt or datetime.now(UTC), attempts=0)
        s.add(m)
        await s.flush()
        mid = m.id
    await own.dispose()
    return mid


async def _fetch(mid: uuid.UUID) -> OutboxMessage:
    own = create_async_engine(settings.sqlalchemy_url)
    async with AsyncSession(own, expire_on_commit=False) as s:
        row = (await s.execute(select(OutboxMessage).where(OutboxMessage.id == mid))).scalar_one()
    await own.dispose()
    return row


def _use_private_engine(monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    """Workers use the module-level engine, which is bound to the TestClient loop — give them their own."""
    own = create_async_engine(settings.sqlalchemy_url)
    maker = async_sessionmaker(own, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(dispatcher, "session_scope", scope)
    monkeypatch.setattr(maintenance, "session_scope", scope)
    return own


async def _run_dispatch(monkeypatch: pytest.MonkeyPatch, ids: dict[str, str], mode: str) -> tuple[uuid.UUID, uuid.UUID, OutboxMessage, OutboxMessage]:
    calls: list[tuple[str, list[str], str]] = []

    async def fake_send(api_key: str, to: list[str], text: str, priority: str = "transactional") -> list[ProviderResult]:
        calls.append((api_key, to, priority))
        if mode == "transient":
            raise XabarchiTransientError("Xabarchi vaqtincha javob bermadi (503)")
        return [ProviderResult(to=to[0], provider_id="777", status="queued", raw={})]

    monkeypatch.setattr(xabarchi, "send_sms", fake_send)
    ok_id = await _insert_outbox(ids["a"], "998900000010")  # company A has a key
    nokey_id = await _insert_outbox(ids["b"], "998900000011")  # company B: provider none
    processed = await dispatcher.dispatch_outbox_once()
    assert processed >= 2
    ok_row = await _fetch(ok_id)
    nokey_row = await _fetch(nokey_id)
    assert nokey_row.status == "failed" and nokey_row.error == "sms_not_configured"
    assert any(call[0] == "xab_test_key" and call[1] == ["998900000010"] for call in calls)
    return ok_id, nokey_id, ok_row, nokey_row


def test_dispatcher_success_and_not_configured(monkeypatch: pytest.MonkeyPatch, ctx: dict) -> None:
    async def run() -> None:
        own = _use_private_engine(monkeypatch)
        _, _, ok_row, _ = await _run_dispatch(monkeypatch, ctx["ids"], "ok")
        assert ok_row.status == "sent" and ok_row.provider_message_id == "777" and ok_row.sent_at is not None
        assert ok_row.leased_until is None and ok_row.error is None
        # already-sent rows are not picked up again
        again = await dispatcher.dispatch_outbox_once()
        assert isinstance(again, int)
        assert (await _fetch(ok_row.id)).attempts == 0
        await own.dispose()

    asyncio.run(run())


def test_dispatcher_transient_backoff(monkeypatch: pytest.MonkeyPatch, ctx: dict) -> None:
    async def run() -> None:
        own = _use_private_engine(monkeypatch)
        _, _, row, _ = await _run_dispatch(monkeypatch, ctx["ids"], "transient")
        assert row.status == "queued" and row.attempts == 1 and row.error and "503" in row.error
        assert row.next_attempt_at is not None
        delay = row.next_attempt_at - row.updated_at
        assert timedelta(seconds=25) <= delay <= timedelta(seconds=35)
        assert dispatcher.backoff_for(1) == timedelta(seconds=30) and dispatcher.backoff_for(5) == timedelta(hours=2) and dispatcher.backoff_for(9) == timedelta(hours=2)
        # not due yet → a second pass leaves it alone
        await dispatcher.dispatch_outbox_once()
        assert (await _fetch(row.id)).attempts == 1
        # maintenance: stale `sending` rows go back to queued, due scheduled rows are promoted
        stale_id = await _insert_outbox(ctx["ids"]["a"], "998900000012", status="sending")
        sched_id = await _insert_outbox(ctx["ids"]["a"], "998900000013", status="scheduled", next_attempt=datetime.now(UTC) - timedelta(minutes=1))
        async with AsyncSession(own, expire_on_commit=False) as s, s.begin():
            stale = await s.get(OutboxMessage, stale_id)
            stale.leased_until = datetime.now(UTC) - timedelta(minutes=20)
            sched = await s.get(OutboxMessage, sched_id)
            sched.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
        result = await maintenance.maintenance_once()
        assert result["stale"] >= 1 and result["promoted"] >= 1
        assert (await _fetch(stale_id)).status == "queued" and (await _fetch(sched_id)).status == "queued"
        await own.dispose()

    asyncio.run(run())
