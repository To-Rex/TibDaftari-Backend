"""Pure rules of per-branch service availability (no DB, no Redis)."""

from __future__ import annotations

import uuid

from app.modules.catalog.service import available_in_branch


def test_service_is_offered_everywhere_unless_the_branch_is_switched_off() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert available_in_branch([], a)
    assert available_in_branch(None, a)
    assert not available_in_branch([a], a)
    assert available_in_branch([a], b)
    # ids arrive as UUIDs from ORM rows and as strings from cached DTOs — both must compare
    assert not available_in_branch([str(a)], a)
    assert not available_in_branch([a], str(a))
    # company-wide lists (no branch in question) hide nothing
    assert available_in_branch([a, b], None)
