"""Organisation-tree endpoints: units (tree, card, create, update), their SMS account / texts, Telegram bot, superadmin reset."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, Meta, Staff
from app.core.exceptions import ValidationError
from app.modules.units import reset, service
from app.modules.units.schemas import (
    OrgUnitOut,
    ResetIn,
    ResetOut,
    ResetPartOut,
    ResetPreviewOut,
    SmsTestIn,
    SmsTestOut,
    TelegramSettingsIn,
    UnitCreateIn,
    UnitSmsIn,
    UnitSmsOut,
    UnitSmsTemplatesIn,
    UnitSmsTemplatesOut,
    UnitTreeQuery,
    UnitUpdateIn,
)

router = APIRouter()

SETTINGS_WRITE = ("admin.settings.write", "admin.unit.write")


@router.get("/units/tree", response_model=list[OrgUnitOut], summary="Every unit the caller may see (flat, with parentId / depth / path)")
async def list_tree(q: Annotated[UnitTreeQuery, Query()], staff: Staff, session: DbSession) -> list[OrgUnitOut]:
    return await service.list_tree(session, staff, q)


@router.post("/units", response_model=OrgUnitOut, status_code=201, summary="Create a unit under a parent (or a country node — platform admin)")
async def create_unit(body: UnitCreateIn, staff: Staff, session: DbSession, meta: Meta) -> OrgUnitOut:
    return await service.create_unit(session, body, staff, meta)


@router.get("/units/{unit_id}", response_model=OrgUnitOut, summary="Unit card (profile, effective settings, SMS / Telegram status, counts)")
async def get_unit(unit_id: uuid.UUID, staff: Staff, session: DbSession) -> OrgUnitOut:
    return await service.get_unit_dto(session, unit_id, staff)


@router.put("/units/{unit_id}", response_model=OrgUnitOut, summary="Update a unit (partial)")
async def update_unit(unit_id: uuid.UUID, body: UnitUpdateIn, staff: Staff, session: DbSession, meta: Meta) -> OrgUnitOut:
    # the settings page (admin.settings.write) may change documentFreeViewDays / locale only; the profile needs admin.unit.write
    if set(body.model_fields_set) <= {"document_free_view_days", "locale"}:
        staff.require(*SETTINGS_WRITE)
    else:
        staff.require("admin.unit.write")
    return await service.update_unit(session, unit_id, body, staff, meta)


@router.get("/units/{unit_id}/sms", response_model=UnitSmsOut, summary="The unit's SMS account (its own Xabarchi key, an inherited one, or off)")
async def get_sms(unit_id: uuid.UUID, staff: Staff, session: DbSession) -> UnitSmsOut:
    staff.require(*SETTINGS_WRITE, "admin.unit.read")
    return await service.get_sms(session, unit_id, staff)


@router.put("/units/{unit_id}/sms", response_model=UnitSmsOut, summary="Save the unit's own SMS account (units below inherit it)")
async def set_sms(unit_id: uuid.UUID, body: UnitSmsIn, staff: Staff, session: DbSession, meta: Meta) -> UnitSmsOut:
    staff.require(*SETTINGS_WRITE)
    return await service.set_sms(session, unit_id, body, staff, meta)


@router.post("/units/{unit_id}/sms/test", response_model=SmsTestOut, summary="Send a real test SMS with the key the unit's messages use")
async def sms_test(unit_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta, body: SmsTestIn | None = None) -> SmsTestOut:
    staff.require(*SETTINGS_WRITE)
    return await service.send_test_sms(session, unit_id, body.to if body else None, staff, meta)


@router.get("/units/{unit_id}/sms-templates", response_model=UnitSmsTemplatesOut, summary="The unit's SMS texts (its own, or inherited)")
async def get_sms_templates(unit_id: uuid.UUID, staff: Staff, session: DbSession) -> UnitSmsTemplatesOut:
    staff.require(*SETTINGS_WRITE, "admin.unit.read")
    return await service.get_sms_templates(session, unit_id, staff)


@router.put("/units/{unit_id}/sms-templates", response_model=UnitSmsTemplatesOut, summary="Save the unit's own SMS texts (applyToAll: the whole subtree)")
async def set_sms_templates(unit_id: uuid.UUID, body: UnitSmsTemplatesIn, staff: Staff, session: DbSession, meta: Meta) -> UnitSmsTemplatesOut:
    staff.require(*SETTINGS_WRITE)
    return await service.set_sms_templates(session, unit_id, body, staff, meta)


@router.put("/units/{unit_id}/telegram", response_model=OrgUnitOut, summary="Connect / disconnect the unit's own Telegram bot")
async def set_telegram(unit_id: uuid.UUID, body: TelegramSettingsIn, staff: Staff, session: DbSession, meta: Meta) -> OrgUnitOut:
    staff.require(*SETTINGS_WRITE)
    return await service.set_telegram(session, unit_id, body, staff, meta)


# ------------------------------------------------------------------ reset ("like newborn") — superadmin, or an installer inside their scope


@router.get("/units/{unit_id}/reset", response_model=ResetPreviewOut, summary="Superadmin / o‘rnatuvchi: what a reset of the unit (and everything under it) would remove — no changes")
async def reset_preview(unit_id: uuid.UUID, staff: Staff, session: DbSession) -> ResetPreviewOut:
    unit = await service.get_unit_or_404(session, unit_id)
    reset.require_reset_right(staff, unit)
    parts, requires = reset.parts_for(unit)
    counts = await reset.preview(session, unit, staff)
    return ResetPreviewOut(
        id=str(unit.id),
        name=unit.name,
        level=unit.level,  # type: ignore[arg-type]
        confirm_word=reset.confirm_word(unit),
        parts=[ResetPartOut(key=k, counts=counts[k], requires=list(requires.get(k, ()))) for k in parts],
    )


@router.post("/units/{unit_id}/reset", response_model=ResetOut, summary="Superadmin / o‘rnatuvchi: irreversibly reset the unit (chosen parts)")
async def reset_unit(unit_id: uuid.UUID, body: ResetIn, staff: Staff, session: DbSession, meta: Meta) -> ResetOut:
    unit = await service.get_unit_or_404(session, unit_id)
    reset.require_reset_right(staff, unit)
    if body.confirm.strip().lower() != reset.confirm_word(unit).lower():
        raise ValidationError(reset.CONFIRM_MISMATCH, code="confirm")
    parts, requires = reset.parts_for(unit)
    chosen = reset.close_parts(body.parts, parts, requires)
    return ResetOut(parts=chosen, counts=await reset.reset_unit(session, unit, chosen, staff, meta))
