"""File delivery (assets, PDFs).

Public files (logos, stamps, template images) are served to anyone — immutable content → long cache. A private file
(a result PDF) is served to a staff member whose scope covers the file's unit (or the path above it — the unit's
ancestors a scoped employee may read) and to a patient who owns a document that references it; everyone else gets
404 so ids do not leak existence.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response as StarletteResponse

from app.api.deps import DbSession, PatientPrincipal, StaffPrincipal, current_patient, current_staff
from app.core.exceptions import AuthError, ForbiddenError, NotFoundError
from app.core.security import decode_token
from app.infrastructure.db.models import StoredFile
from app.modules.files.service import content_disposition, get_file, patient_owns_file
from app.modules.portal.service import identity_ids
from app.modules.units import repository as units_repo

router = APIRouter()

Principal = StaffPrincipal | PatientPrincipal | None


async def _principal(session: AsyncSession, authorization: str | None) -> Principal:
    """The caller behind an optional bearer token — staff or patient; anonymous (None) when absent or invalid."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        claims = decode_token(authorization[7:].strip())
        if claims.get("act") == "staff":
            return await current_staff(session, authorization)
        if claims.get("act") == "patient":
            return await current_patient(session, authorization)
    except (AuthError, ForbiddenError):
        return None
    return None


async def _may_read(session: AsyncSession, row: StoredFile, who: Principal) -> bool:
    if isinstance(who, StaffPrincipal):
        if row.unit_id is None or who.can_see(row.unit_id):
            return True
        # the path above the employee's units (context they may read: the region's logo, the country's stamp …)
        return row.unit_id in await units_repo.ancestors_of(session, who.unit_ids)
    if isinstance(who, PatientPrincipal):
        return await patient_owns_file(session, row.id, await identity_ids(session, who))
    return False


@router.get("/files/{file_id}", summary="Download a stored file (asset / PDF)")
async def download(file_id: uuid.UUID, session: DbSession, authorization: Annotated[str | None, Header()] = None) -> StarletteResponse:
    row = await get_file(session, file_id)
    if not row or row.data is None:
        raise NotFoundError("Fayl topilmadi")
    if row.is_public:
        headers = {"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{row.sha256[:32]}"'}
    else:
        if not await _may_read(session, row, await _principal(session, authorization)):
            raise NotFoundError("Fayl topilmadi")
        headers = {"Cache-Control": "private, max-age=3600", "ETag": f'"{row.sha256[:32]}"'}
    if row.mime == "image/svg+xml":
        # Client-supplied SVG may carry scripts: never let it execute on the API origin.
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    if row.filename:
        headers["Content-Disposition"] = content_disposition(row.filename)
    return Response(content=bytes(row.data), media_type=row.mime, headers=headers)
