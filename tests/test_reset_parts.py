"""Superadmin reset: part selection rules (pure — no database)."""

from __future__ import annotations

import pytest
from app.core.exceptions import ValidationError
from app.modules.units.reset import FACILITY_PARTS, FACILITY_REQUIRES, UNIT_PARTS, UNIT_REQUIRES, close_parts


def test_unit_dependencies_are_added_in_execution_order() -> None:
    assert close_parts(["patients"], UNIT_PARTS, UNIT_REQUIRES) == ["orders", "patients"]
    assert close_parts(["catalog"], UNIT_PARTS, UNIT_REQUIRES) == ["orders", "templates", "catalog"]
    assert close_parts(["units"], UNIT_PARTS, UNIT_REQUIRES) == ["orders", "patients", "units"]
    # a document names its template and a cheque its employee: both pull the cheques in
    assert close_parts(["templates"], UNIT_PARTS, UNIT_REQUIRES) == ["orders", "templates"]
    assert close_parts(["staff"], UNIT_PARTS, UNIT_REQUIRES) == ["orders", "staff"]
    assert close_parts(list(UNIT_PARTS), UNIT_PARTS, UNIT_REQUIRES) == list(UNIT_PARTS)


def test_facility_dependencies_and_validation() -> None:
    assert close_parts(["patients"], FACILITY_PARTS, FACILITY_REQUIRES) == ["orders", "patients"]
    assert close_parts(["templates"], FACILITY_PARTS, FACILITY_REQUIRES) == ["orders", "templates"]
    assert close_parts(["prices", "staff"], FACILITY_PARTS, FACILITY_REQUIRES) == ["orders", "staff", "prices"]
    assert close_parts(["prices"], FACILITY_PARTS, FACILITY_REQUIRES) == ["prices"]
    with pytest.raises(ValidationError):
        close_parts([], FACILITY_PARTS, FACILITY_REQUIRES)
    with pytest.raises(ValidationError):
        close_parts(["units"], FACILITY_PARTS, FACILITY_REQUIRES)  # a facility reset cannot delete units
    with pytest.raises(ValidationError):
        close_parts(["catalog"], FACILITY_PARTS, FACILITY_REQUIRES)  # the catalogue is reset from the owning unit
