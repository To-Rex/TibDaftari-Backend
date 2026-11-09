"""API + worker tests for the messaging module against the test database (fixture rows prefixed T-messaging-).

The SMS account sits on the region node of tree A and is inherited by its facility; tree B has none."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.core.crypto import encrypt
from app.infrastructure.db.models import MessagingAccount, Notification, OutboxMessage
from app.modules.messaging import dispatcher, maintenance, xabarchi
from app.modules.messaging.xabarchi import ProviderResult, XabarchiTransientError
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.helpers import (
    API,
    app_instance,
    bearer,
    make_employee,
    make_role,
    make_tree,
    own_engine,
    own_sessionmaker,
    seed,
    staff_token,
)

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-messaging-pass-1"


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        s.add(MessagingAccount(unit_id=a.region.id, channel="sms", provider="xabarchi", api_key_enc=encrypt("xab_test_key"), api_key_masked="xab_••••", default_priority="transactional"))
        sender = await make_role(s, a.region, "sender", "T-messaging Sender", ["messaging.send"])
        admin_a = await make_employee(s, a.region, "admin", login=f"t-messaging-admin-a-{SFX}", password=PASSWORD)
        await make_employee(s, b.region, "admin", login=f"t-messaging-admin-b-{SFX}", password=PASSWORD)
        sender_a = await make_employee(s, a.facility, role=sender, login=f"t-messaging-sender-a-{SFX}", password=PASSWORD)
        s.add_all(
            [
                Notification(unit_id=a.region.id, title="T-messaging unit-wide", body="for everyone under the region", kind="info", read_by=[]),
                Notification(unit_id=a.region.id, employee_id=admin_a.id, title="T-messaging personal", body="admin only", kind="warning", read_by=[]),
                Notification(unit_id=a.facility.id, employee_id=sender_a.id, title="T-messaging other person", body="sender only", kind="success", read_by=[]),
                Notification(unit_id=b.region.id, title="T-messaging unit B", body="other tree", kind="info", read_by=[]),
            ]
        )
        await s.flush()
        return {
            "a": {k: str(getattr(a, k).id) for k in ("country", "region", "district", "facility")},
            "b": {k: str(getattr(b, k).id) for k in ("country", "region", "district", "facility")},
            "tag": a.tag,
            "admin_a": str(admin_a.id),
            "sender_a": str(sender_a.id),
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, f"t-messaging-{who}-{SFX}", PASSWORD) for who in ("admin-a", "admin-b", "sender-a")}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


OUTBOX_KEYS = {
    "id", "unitId", "unitName", "patientId", "orderId", "channel", "kind", "to", "text", "status",
    "scheduledAt", "sentAt", "attempts", "providerMessageId", "error", "createdAt", "updatedAt",
}  # fmt: skip


def test_send_and_list_outbox(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a = ctx["a"]
    fac, region = a["facility"], a["region"]
    # broadcast (2 distinct + 1 duplicate + 1 invalid) by admin, from the facility
    r = c.post(
        f"{API}/units/{fac}/messages/send",
        json={"to": ["+998 90 123-45-67", "901234567", "998911112233", "12345"], "text": f"T-messaging salom {SFX}", "kind": "broadcast"},
        headers=h(ctx, "admin-a"),
    )
    assert r.status_code == 201, r.text
    msgs = r.json()
    assert len(msgs) == 2 and {m["to"] for m in msgs} == {"998901234567", "998911112233"}
    assert set(msgs[0]) == OUTBOX_KEYS
    assert msgs[0]["status"] == "queued" and msgs[0]["channel"] == "sms" and msgs[0]["attempts"] == 0
    assert msgs[0]["unitId"] == fac and msgs[0]["unitName"] == f"T-{ctx['tag']} SES"
    assert r.headers.get("X-Invalid-Recipients") == "12345"
    # from the region the facility must be named (a facility under it)
    r = c.post(f"{API}/units/{region}/messages/send", json={"to": ["998900000005"], "text": "T-messaging region"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "facility_required"
    r = c.post(f"{API}/units/{region}/messages/send", json={"to": ["998900000005"], "text": "T-messaging region", "unitId": fac}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201 and r.json()[0]["unitId"] == fac
    r = c.post(f"{API}/units/{region}/messages/send", json={"to": ["998900000005"], "text": "x", "unitId": ctx["b"]["facility"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Muassasa topilmadi"
    # scheduled in the future
    future = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = c.post(f"{API}/units/{fac}/messages/send", json={"to": ["998900000001"], "text": "T-messaging later", "kind": "reminder", "scheduledAt": future}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201 and r.json()[0]["status"] == "scheduled" and r.json()[0]["scheduledAt"] == future
    # sender without messaging.broadcast: 1 recipient ok, 2 → 403
    assert c.post(f"{API}/units/{fac}/messages/send", json={"to": ["998900000002"], "text": "T-messaging one", "kind": "broadcast"}, headers=h(ctx, "sender-a")).status_code == 201
    r = c.post(f"{API}/units/{fac}/messages/send", json={"to": ["998900000002", "998900000003"], "text": "T-messaging two", "kind": "broadcast"}, headers=h(ctx, "sender-a"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    # all invalid → 422 invalid_phone; empty text → 422
    r = c.post(f"{API}/units/{fac}/messages/send", json={"to": ["abc"], "text": "x", "kind": "broadcast"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_phone"
    assert c.post(f"{API}/units/{fac}/messages/send", json={"to": ["998900000002"], "text": "", "kind": "broadcast"}, headers=h(ctx, "admin-a")).status_code == 422
    # unit isolation
    assert c.post(f"{API}/units/{fac}/messages/send", json={"to": ["998900000002"], "text": "x", "kind": "broadcast"}, headers=h(ctx, "admin-b")).status_code == 403
    assert c.get(f"{API}/units/{fac}/outbox", headers=h(ctx, "admin-b")).status_code == 403
    # list from the region (everything under it): newest first, filters, search by digits and by folded text
    r = c.get(f"{API}/units/{region}/outbox", headers=h(ctx, "admin-a"))
    assert r.status_code == 200
    page = r.json()
    assert set(page) == {"items", "page", "pageSize", "total", "totalPages"} and page["total"] >= 5
    created = [m["createdAt"] for m in page["items"]]
    assert created == sorted(created, reverse=True)
    assert c.get(f"{API}/units/{region}/outbox", params={"unitId": fac}, headers=h(ctx, "admin-a")).json()["total"] == page["total"]
    assert c.get(f"{API}/units/{region}/outbox", params={"unitId": ctx["b"]["facility"]}, headers=h(ctx, "admin-a")).json()["total"] == 0
    r = c.get(f"{API}/units/{region}/outbox", params={"status": "scheduled", "kind": "reminder"}, headers=h(ctx, "admin-a"))
    assert r.json()["total"] == 1 and r.json()["items"][0]["to"] == "998900000001"
    r = c.get(f"{API}/units/{region}/outbox", params={"search": "91 111"}, headers=h(ctx, "admin-a"))
    assert {m["to"] for m in r.json()["items"]} == {"998911112233"}
    r = c.get(f"{API}/units/{region}/outbox", params={"search": f"SALOM {SFX}"}, headers=h(ctx, "admin-a"))
    assert r.json()["total"] == 2
    assert c.get(f"{API}/units/{region}/outbox", params={"status": "bogus"}, headers=h(ctx, "admin-a")).status_code == 422
    counts = c.get(f"{API}/units/{region}/outbox/counts", headers=h(ctx, "admin-a")).json()
    assert counts["all"] == page["total"] and counts["scheduled"] == 1 and counts["queued"] >= 4
    # the sender may list (messaging.send) — their own facility
    assert c.get(f"{API}/units/{fac}/outbox", headers=h(ctx, "sender-a")).status_code == 200
    assert c.get(f"{API}/units/{region}/outbox", headers=h(ctx, "sender-a")).status_code == 403


def test_notifications_and_mark_read(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    r = c.get(f"{API}/notifications", headers=h(ctx, "admin-a"))
    assert r.status_code == 200
    items = [n for n in r.json() if n["title"].startswith("T-messaging")]
    assert {n["title"] for n in items} == {"T-messaging unit-wide", "T-messaging personal"}
    assert set(items[0]) == {"id", "title", "body", "kind", "createdAt", "read", "link"}
    assert all(n["read"] is False for n in items)
    personal = next(n for n in items if n["title"] == "T-messaging personal")
    assert c.post(f"{API}/notifications/read", json={"id": personal["id"]}, headers=h(ctx, "admin-a")).status_code == 200
    items = {n["title"]: n for n in c.get(f"{API}/notifications", headers=h(ctx, "admin-a")).json() if n["title"].startswith("T-messaging")}
    assert items["T-messaging personal"]["read"] is True and items["T-messaging unit-wide"]["read"] is False
    # the sender at the facility: the region-wide notice reaches them (read_by is per employee), plus their own
    sender_items = {n["title"]: n for n in c.get(f"{API}/notifications", headers=h(ctx, "sender-a")).json() if n["title"].startswith("T-messaging")}
    assert set(sender_items) == {"T-messaging unit-wide", "T-messaging other person"} and sender_items["T-messaging unit-wide"]["read"] is False
    # mark all
    assert c.post(f"{API}/notifications/read", json={}, headers=h(ctx, "admin-a")).status_code == 200
    items = [n for n in c.get(f"{API}/notifications", headers=h(ctx, "admin-a")).json() if n["title"].startswith("T-messaging")]
    assert all(n["read"] for n in items)
    # tree B never sees A's notifications
    titles_b = {n["title"] for n in c.get(f"{API}/notifications", headers=h(ctx, "admin-b")).json()}
    assert "T-messaging unit B" in titles_b and not (titles_b & {"T-messaging unit-wide", "T-messaging personal"})


# ----------------------------------------------------------------------------- dispatcher (worker) tests


async def _insert_outbox(own: AsyncEngine, unit_id: str, to: str, *, status: str = "queued", next_attempt: datetime | None = None) -> uuid.UUID:
    async with AsyncSession(own, expire_on_commit=False) as s, s.begin():
        m = OutboxMessage(unit_id=uuid.UUID(unit_id), channel="sms", kind="broadcast", to=to, text="T-messaging dispatch", status=status, next_attempt_at=next_attempt or datetime.now(UTC), attempts=0)
        s.add(m)
        await s.flush()
        return m.id


async def _fetch(own: AsyncEngine, mid: uuid.UUID) -> OutboxMessage:
    async with AsyncSession(own, expire_on_commit=False) as s:
        return (await s.execute(select(OutboxMessage).where(OutboxMessage.id == mid))).scalar_one()


def _use_private_engine(monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    """Workers use the module-level engine, which is bound to the TestClient loop — give them their own."""
    own = own_engine()
    maker = own_sessionmaker(own)

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


async def _run_dispatch(own: AsyncEngine, monkeypatch: pytest.MonkeyPatch, ctx: dict, mode: str) -> tuple[OutboxMessage, OutboxMessage]:
    calls: list[tuple[str, list[str], str]] = []

    async def fake_send(api_key: str, to: list[str], text: str, priority: str = "transactional") -> list[ProviderResult]:
        calls.append((api_key, to, priority))
        if mode == "transient":
            raise XabarchiTransientError("Xabarchi vaqtincha javob bermadi (503)")
        return [ProviderResult(to=to[0], provider_id="777", status="queued", raw={})]

    monkeypatch.setattr(xabarchi, "send_sms", fake_send)
    ok_id = await _insert_outbox(own, ctx["a"]["facility"], "998900000010")  # facility A inherits the region's key
    nokey_id = await _insert_outbox(own, ctx["b"]["facility"], "998900000011")  # tree B: nothing configured
    processed = await dispatcher.dispatch_outbox_once()
    assert processed >= 2
    ok_row = await _fetch(own, ok_id)
    nokey_row = await _fetch(own, nokey_id)
    assert nokey_row.status == "failed" and nokey_row.error == "sms_not_configured"
    assert any(call[0] == "xab_test_key" and call[1] == ["998900000010"] for call in calls)
    return ok_row, nokey_row


def test_dispatcher_success_and_not_configured(monkeypatch: pytest.MonkeyPatch, ctx: dict) -> None:
    async def run() -> None:
        own = _use_private_engine(monkeypatch)
        try:
            ok_row, _ = await _run_dispatch(own, monkeypatch, ctx, "ok")
            assert ok_row.status == "sent" and ok_row.provider_message_id == "777" and ok_row.sent_at is not None
            assert ok_row.leased_until is None and ok_row.error is None
            # already-sent rows are not picked up again
            again = await dispatcher.dispatch_outbox_once()
            assert isinstance(again, int)
            assert (await _fetch(own, ok_row.id)).attempts == 0
        finally:
            await own.dispose()

    asyncio.run(run())


def test_dispatcher_transient_backoff(monkeypatch: pytest.MonkeyPatch, ctx: dict) -> None:
    async def run() -> None:
        own = _use_private_engine(monkeypatch)
        try:
            row, _ = await _run_dispatch(own, monkeypatch, ctx, "transient")
            assert row.status == "queued" and row.attempts == 1 and row.error and "503" in row.error
            assert row.next_attempt_at is not None
            delay = row.next_attempt_at - row.updated_at
            assert timedelta(seconds=25) <= delay <= timedelta(seconds=35)
            assert dispatcher.backoff_for(1) == timedelta(seconds=30) and dispatcher.backoff_for(5) == timedelta(hours=2) and dispatcher.backoff_for(9) == timedelta(hours=2)
            # not due yet → a second pass leaves it alone
            await dispatcher.dispatch_outbox_once()
            assert (await _fetch(own, row.id)).attempts == 1
            # maintenance: stale `sending` rows go back to queued, due scheduled rows are promoted
            stale_id = await _insert_outbox(own, ctx["a"]["facility"], "998900000012", status="sending")
            sched_id = await _insert_outbox(own, ctx["a"]["facility"], "998900000013", status="scheduled", next_attempt=datetime.now(UTC) - timedelta(minutes=1))
            async with AsyncSession(own, expire_on_commit=False) as s, s.begin():
                stale = await s.get(OutboxMessage, stale_id)
                stale.leased_until = datetime.now(UTC) - timedelta(minutes=20)
                sched = await s.get(OutboxMessage, sched_id)
                sched.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
            result = await maintenance.maintenance_once()
            assert result["stale"] >= 1 and result["promoted"] >= 1
            assert (await _fetch(own, stale_id)).status == "queued" and (await _fetch(own, sched_id)).status == "queued"
        finally:
            await own.dispose()

    asyncio.run(run())
