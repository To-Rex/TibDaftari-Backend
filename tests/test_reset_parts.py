"""Superadmin reset: part selection rules (pure — no database)."""

from __future__ import annotations

import pytest
from app.core.exceptions import ValidationError
from app.modules.tenant.reset import BRANCH_PARTS, BRANCH_REQUIRES, COMPANY_PARTS, COMPANY_REQUIRES, close_parts


def test_company_dependencies_are_added_in_execution_order() -> None:
    assert close_parts(["patients"], COMPANY_PARTS, COMPANY_REQUIRES) == ["orders", "patients"]
    assert close_parts(["catalog"], COMPANY_PARTS, COMPANY_REQUIRES) == ["orders", "templates", "catalog"]
    assert close_parts(["branches"], COMPANY_PARTS, COMPANY_REQUIRES) == ["orders", "patients", "branches"]
    assert close_parts(["staff"], COMPANY_PARTS, COMPANY_REQUIRES) == ["staff"]
    assert close_parts(list(COMPANY_PARTS), COMPANY_PARTS, COMPANY_REQUIRES) == list(COMPANY_PARTS)


def test_branch_dependencies_and_validation() -> None:
    assert close_parts(["patients"], BRANCH_PARTS, BRANCH_REQUIRES) == ["orders", "patients"]
    assert close_parts(["prices", "staff"], BRANCH_PARTS, BRANCH_REQUIRES) == ["staff", "prices"]
    with pytest.raises(ValidationError):
        close_parts([], BRANCH_PARTS, BRANCH_REQUIRES)
    with pytest.raises(ValidationError):
        close_parts(["branches"], BRANCH_PARTS, BRANCH_REQUIRES)  # a branch reset cannot delete branches
