"""Result trail of an order item (pure — no database): legacy items, new events, the DTO."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.modules.orders.service import _push_history, _seed_history, item_history

T1 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 1, 10, 30, tzinfo=UTC)
T3 = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
DOC = uuid.uuid4()
DOCTOR = SimpleNamespace(id=uuid.uuid4(), employee=SimpleNamespace(full_name="Dr Aliyev"))


def _item(**kw):
    base = dict(history=[], status="entered", submitted_at=None, approved_at=None, technician_id=uuid.uuid4(), technician_name="Laborant",
                doctor_id=None, doctor_name=None, document_id=None, reject_reason=None, updated_at=T2)
    return SimpleNamespace(**{**base, **kw})


def test_legacy_trail_is_rebuilt_from_timestamps() -> None:
    it = _item(status="approved", submitted_at=T1, approved_at=T2, doctor_id=DOCTOR.id, doctor_name="Dr Aliyev", document_id=DOC)
    ev = item_history(it)
    assert [e["type"] for e in ev] == ["submitted", "approved"]
    assert ev[0]["byName"] == "Laborant" and ev[1]["byName"] == "Dr Aliyev" and ev[1]["documentId"] == str(DOC)
    returned = item_history(_item(status="rejected", reject_reason="Gemoglobin xato"))
    assert returned == [{"type": "returned", "at": "2026-10-01T10:30:00.000Z", "reason": "Gemoglobin xato"}]
    assert item_history(_item()) == []


def test_new_events_keep_the_legacy_trail() -> None:
    it = _item(status="approved", submitted_at=T1, approved_at=T2, doctor_name="Dr Aliyev", document_id=DOC)
    _seed_history(it)  # before the state changes
    it.status, it.approved_at, it.reject_reason = "rejected", None, "Birlik noto‘g‘ri"
    _push_history(it, "revoked", DOCTOR, T3, reason="Birlik noto‘g‘ri", document_id=DOC)
    assert [e["type"] for e in it.history] == ["submitted", "approved", "revoked"]
    last = it.history[-1]
    assert last == {"type": "revoked", "at": "2026-10-02T08:00:00.000Z", "byId": str(DOCTOR.id), "byName": "Dr Aliyev", "reason": "Birlik noto‘g‘ri", "documentId": str(DOC)}
    # a stored trail wins over the timestamps from now on
    it.status, it.submitted_at = "submitted", T3
    _push_history(it, "submitted", DOCTOR, T3)
    assert [e["type"] for e in item_history(it)] == ["submitted", "approved", "revoked", "submitted"]


def test_many_returns_and_submissions_are_all_kept() -> None:
    it = _item()
    for k in range(3):
        _push_history(it, "submitted", DOCTOR, T1)
        _push_history(it, "returned", DOCTOR, T2, reason=f"xato {k + 1}")
    _push_history(it, "submitted", DOCTOR, T3)
    _push_history(it, "approved", DOCTOR, T3, document_id=DOC)
    types = [e["type"] for e in it.history]
    assert types.count("submitted") == 4 and types.count("returned") == 3 and types[-1] == "approved"
    assert [e.get("reason") for e in it.history if e["type"] == "returned"] == ["xato 1", "xato 2", "xato 3"]


def test_first_event_is_not_doubled() -> None:
    # a fresh item: seeding before the change finds nothing, the new step is stored once
    it = _item()
    _seed_history(it)
    it.status, it.submitted_at = "submitted", T1
    _push_history(it, "submitted", DOCTOR, T1)
    assert [e["type"] for e in it.history] == ["submitted"]
    # a trail stored by the first release (rebuilt step + identical new step) is shown once
    dup = _item(history=[{"type": "submitted", "at": "2026-10-01T09:00:00.000Z", "byName": "Laborant"}, {"type": "submitted", "at": "2026-10-01T09:00:00.000Z", "byName": "Laborant"}, {"type": "approved", "at": "2026-10-01T10:30:00.000Z"}])
    assert [e["type"] for e in item_history(dup)] == ["submitted", "approved"]
