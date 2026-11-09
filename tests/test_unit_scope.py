"""`StaffPrincipal` unit isolation and `UnitScope` — the rules every list / row endpoint applies (pure — no DB)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.api.deps import StaffPrincipal
from app.core.exceptions import ForbiddenError
from app.core.scope import UnitScope

ROOT, REGION, DISTRICT, FAC1, FAC2, OTHER = (uuid.uuid4() for _ in range(6))


def _staff(roots: list[uuid.UUID], allowed: set[uuid.UUID] | None, *, superadmin: bool = False) -> StaffPrincipal:
    emp = SimpleNamespace(id=uuid.uuid4(), unit_id=roots[0] if roots else None, is_super_admin=superadmin)
    return StaffPrincipal(
        employee=emp, role=SimpleNamespace(key="admin"), permissions=[], jti="t", token_exp=datetime.now(UTC),
        unit_ids=roots, allowed_unit_ids=None if allowed is None else frozenset(allowed),
    )  # type: ignore[arg-type]


def test_scoped_employee_acts_only_inside_their_subtrees() -> None:
    one = _staff([DISTRICT], {DISTRICT, FAC1, FAC2})
    assert one.home_unit_id == DISTRICT and one.home_facility_id is None
    assert one.can_see(DISTRICT) and one.can_see(FAC1) and one.can_see(str(FAC2))
    assert not one.can_see(REGION) and not one.can_see(OTHER) and not one.can_see(None) and not one.can_see("not-a-uuid")
    assert one.can_see_any([OTHER, FAC1]) and not one.can_see_any([OTHER, REGION])
    assert one.require_unit(FAC1) is one
    with pytest.raises(ForbiddenError) as e:
        one.require_unit(OTHER)
    assert e.value.message == "Ruxsat yo‘q"
    # list filters are cut down to the allowed set; nothing left → a list that matches nothing
    assert one.restrict([FAC1, OTHER]) == [FAC1]
    assert one.restrict([OTHER]) == [uuid.UUID(int=0)]
    # two scope roots (home + an extra unit)
    two = _staff([FAC1, FAC2], {FAC1, FAC2})
    assert two.unit_ids == [FAC1, FAC2] and two.can_see(FAC2) and not two.can_see(DISTRICT)


def test_superadmin_acts_everywhere() -> None:
    sup = _staff([], None, superadmin=True)
    assert sup.can_see(OTHER) and sup.can_see(None) and sup.can_see_any([]) and sup.require_unit(OTHER) is sup
    assert sup.restrict([OTHER, FAC1]) == [OTHER, FAC1]
    assert sup.has("platform.unit.manage") and sup.allowed_unit_ids is None
    # an employee without a resolved allowed set (no units at all) sees nothing
    nobody = _staff([], None)
    assert not nobody.can_see(FAC1) and nobody.restrict([FAC1]) == [uuid.UUID(int=0)]


def test_unit_scope_sets() -> None:
    unit = SimpleNamespace(id=DISTRICT, country_id=uuid.uuid4(), level="district")
    scope = UnitScope(unit=unit, descendant_ids=[DISTRICT, FAC1, FAC2], ancestor_ids=[ROOT, REGION, DISTRICT], facility_ids=[FAC1, FAC2])  # type: ignore[arg-type]
    assert scope.id == DISTRICT and scope.country_id == unit.country_id and not scope.is_facility
    # management view of the catalogue: ancestors (without self) + the subtree
    assert scope.lineage_ids == [ROOT, REGION, DISTRICT, FAC1, FAC2]
    assert scope.contains(FAC1) and scope.contains(DISTRICT) and not scope.contains(REGION) and not scope.contains(None)
    # a `unitId` narrowing: ids outside the scope match nothing, no ids → the whole scope
    assert scope.narrow(None) == [DISTRICT, FAC1, FAC2] and scope.narrow([]) == [DISTRICT, FAC1, FAC2]
    assert scope.narrow([FAC2]) == [FAC2] and scope.narrow([FAC2, OTHER]) == [FAC2]
    assert scope.narrow([OTHER]) == [uuid.UUID(int=0)]
    facility = UnitScope(unit=SimpleNamespace(id=FAC1, country_id=unit.country_id, level="facility"), descendant_ids=[FAC1], ancestor_ids=[ROOT, REGION, DISTRICT, FAC1], facility_ids=[FAC1])  # type: ignore[arg-type]
    assert facility.is_facility and facility.lineage_ids == [ROOT, REGION, DISTRICT, FAC1]
