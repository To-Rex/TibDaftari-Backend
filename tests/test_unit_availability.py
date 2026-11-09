"""Per-unit service availability and prices (`service_unit_settings`): the nearest level on the facility's path
wins; a service is offered where it is visible (owner on the path) unless switched off (pure — no DB)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.infrastructure.db.models import ServiceUnitSetting
from app.modules.catalog.service import _resolve_on_path, settings_view

ROOT, REGION, DISTRICT, FAC = (uuid.uuid4() for _ in range(4))
PATH = [ROOT, REGION, DISTRICT, FAC]
S1, S2 = uuid.uuid4(), uuid.uuid4()


def _row(service: uuid.UUID, unit: uuid.UUID, *, enabled: bool = True, price: int | None = None) -> ServiceUnitSetting:
    return ServiceUnitSetting(service_type_id=service, unit_id=unit, is_enabled=enabled, price=price, updated_at=datetime.now(UTC))


def test_service_is_offered_everywhere_unless_switched_off_on_the_path() -> None:
    defaults = {S1: (True, 100_000), S2: (True, 60_000)}
    assert _resolve_on_path(PATH, [], defaults) == {S1: (True, 100_000), S2: (True, 60_000)}
    # switched off at the district → off for the facility (and the price stays the base price)
    assert _resolve_on_path(PATH, [_row(S1, DISTRICT, enabled=False)], defaults)[S1] == (False, 100_000)
    # a row of a unit off the path changes nothing
    assert _resolve_on_path(PATH, [_row(S1, uuid.uuid4(), enabled=False)], defaults)[S1] == (True, 100_000)
    # the nearest level wins: off at the region, on again at the facility
    rows = [_row(S1, REGION, enabled=False), _row(S1, FAC, enabled=True)]
    assert _resolve_on_path(PATH, rows, defaults)[S1] == (True, 100_000)
    # an inactive / invisible service is never offered, whatever the rows say
    assert _resolve_on_path(PATH, [_row(S2, FAC, enabled=True)], {S2: (False, 60_000)})[S2] == (False, 60_000)


def test_nearest_price_wins_and_on_off_is_resolved_separately() -> None:
    defaults = {S1: (True, 100_000)}
    # region price, facility only switches off: the price still comes from the region row
    rows = [_row(S1, REGION, price=80_000), _row(S1, FAC, enabled=False)]
    assert _resolve_on_path(PATH, rows, defaults)[S1] == (False, 80_000)
    # facility price beats the region price
    rows = [_row(S1, REGION, price=80_000), _row(S1, FAC, price=50_000)]
    assert _resolve_on_path(PATH, rows, defaults)[S1] == (True, 50_000)
    # a row without a price keeps the base price
    assert _resolve_on_path(PATH, [_row(S1, FAC)], defaults)[S1] == (True, 100_000)


def test_settings_view_is_what_the_wire_shows() -> None:
    rows = [_row(S1, REGION, price=80_000), _row(S1, FAC, enabled=False), _row(S1, DISTRICT, enabled=False, price=70_000)]
    prices, disabled = settings_view(rows)
    assert prices == {str(REGION): 80_000, str(DISTRICT): 70_000}
    assert disabled == [str(FAC), str(DISTRICT)]
    assert settings_view([]) == ({}, [])
