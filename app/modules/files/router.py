"""Public file delivery (assets, PDFs). Immutable content → long cache."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response
from starlette.responses import Response as StarletteResponse

from app.api.deps import DbSession
from app.core.exceptions import NotFoundError
from app.modules.files.service import get_file

router = APIRouter()


@router.get("/files/{file_id}", summary="Download a stored file (asset / PDF)")
async def download(file_id: uuid.UUID, session: DbSession) -> StarletteResponse:
    row = await get_file(session, file_id)
    if not row or not row.is_public:
        # Private files (result PDFs) are served only through the authenticated / tokenised document endpoints.
        raise NotFoundError("Fayl topilmadi")
    headers = {"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{row.sha256[:32]}"'}
    if row.mime == "image/svg+xml":
        # Client-supplied SVG may carry scripts: never let it execute on the API origin.
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    if row.filename:
        headers["Content-Disposition"] = f'inline; filename="{row.filename}"'
    return Response(content=bytes(row.data), media_type=row.mime, headers=headers)
