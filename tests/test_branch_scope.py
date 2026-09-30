"""StaffPrincipal.branch_scope — the one rule every list endpoint uses to keep other branches' data out."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.api.deps import StaffPrincipal

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _staff(branches: list[uuid.UUID], *, role: str = "reception", superadmin: bool = False) -> StaffPrincipal:
    emp = SimpleNamespace(id=uuid.uuid4(), company_id=uuid.uuid4(), branch_ids=branches, is_super_admin=superadmin)
    return StaffPrincipal(employee=emp, role=SimpleNamespace(key=role), permissions=[], jti="t", token_exp=datetime.now(UTC))  # type: ignore[arg-type]


def test_pinned_employee_is_confined_to_own_branches() -> None:
    one = _staff([A])
    assert one.branch_scope() == [A] and one.branch_scope(A) == [A] and one.branch_scope(str(A)) == [A]
    assert one.branch_scope(B) == [A]  # another branch is never honoured
    assert one.branch_scope("not-a-uuid") == [A]
    assert one.allows_branch(A) and not one.allows_branch(B) and not one.allows_branch(None)
    two = _staff([A, B])
    assert two.branch_scope() == [A, B] and two.branch_scope(B) == [B] and two.branch_scope(C) == [A, B]


def test_switchers_and_branchless_are_not_confined() -> None:
    for s in (_staff([A], role="admin"), _staff([], superadmin=True), _staff([])):
        assert s.branch_scope() is None and s.branch_scope(B) == [B] and s.allows_branch(C)
        assert s.branch_scope("not-a-uuid") == [uuid.UUID(int=0)]
    assert _staff([A], role="admin").can_switch_branch and not _staff([A]).can_switch_branch
