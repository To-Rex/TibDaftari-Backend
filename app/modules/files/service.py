"""Binary storage in Postgres (`stored_files`) — assets (logo/stamp/signature) and generated PDFs.

Files belong to a unit (`unit_id`, NULL = system-level) and are content-addressed inside it (sha256) so
re-uploading the same image reuses the row. Public URL: `/api/v1/files/{id}` (ids are UUIDv7 → unguessable enough
for logos); private files (result PDFs) are served to a staff member whose scope covers the unit or to the patient
who owns the document — see `files.router`.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import uuid
from collections.abc import Sequence
from urllib.parse import quote, unquote_to_bytes

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.core.textutil import slugify
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Order, ResultDocument, StoredFile

_DATA_URL = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?P<params>(;[\w-]+=[\w-]+)*)(?P<b64>;base64)?,(?P<data>.*)$", re.S)
ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/bmp", "image/svg+xml"}
MAX_IMAGE_BYTES = 10_000_000


def file_url(file_id: uuid.UUID | str) -> str:
    return f"/api/v1/files/{file_id}"


def absolute_file_url(file_id: uuid.UUID | str) -> str:
    return settings.public_api_url.rstrip("/") + file_url(file_id)


def decode_data_url(data_url: str) -> tuple[bytes, str]:
    m = _DATA_URL.match(data_url.strip())
    if not m:
        raise ValidationError("Rasm formati noto‘g‘ri (data URL kutilgan)")
    mime = m.group("mime") or "application/octet-stream"
    raw = m.group("data")
    data = base64.b64decode(raw, validate=False) if m.group("b64") else unquote_to_bytes(raw)
    return data, mime


def image_size(data: bytes, mime: str) -> tuple[float, float] | None:
    if mime == "image/svg+xml":
        text = data[:4000].decode("utf-8", "ignore")
        w = re.search(r'\swidth="([\d.]+)', text)
        h = re.search(r'\sheight="([\d.]+)', text)
        if w and h:
            return float(w.group(1)), float(h.group(1))
        vb = re.search(r'viewBox="[\d.\-]+\s+[\d.\-]+\s+([\d.]+)\s+([\d.]+)"', text)
        return (float(vb.group(1)), float(vb.group(2))) if vb else None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return float(im.width), float(im.height)
    except Exception:
        return None


async def store_bytes(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID | None,
    data: bytes,
    mime: str,
    filename: str | None = None,
    created_by: uuid.UUID | None = None,
    is_public: bool = False,
) -> StoredFile:
    sha = hashlib.sha256(data).hexdigest()
    # Dedupe by hash inside the unit without pulling the existing blob into memory (only the id is needed).
    owner = StoredFile.unit_id.is_(None) if unit_id is None else StoredFile.unit_id == unit_id
    existing_id = (await session.execute(select(StoredFile.id).where(owner, StoredFile.sha256 == sha).limit(1))).scalar_one_or_none()
    if existing_id:
        existing = await session.get(StoredFile, existing_id, options=[defer(StoredFile.data)])
        if existing:
            return existing
    row = StoredFile(unit_id=unit_id, sha256=sha, mime=mime, size=len(data), filename=filename, storage="db", data=data, is_public=is_public, created_by=created_by)
    session.add(row)
    await session.flush()
    return row


async def store_data_url(session: AsyncSession, *, unit_id: uuid.UUID | None, data_url: str, filename: str | None = None, created_by: uuid.UUID | None = None) -> tuple[StoredFile, tuple[float, float] | None]:
    data, mime = decode_data_url(data_url)
    if mime not in ALLOWED_IMAGE_MIMES:
        raise ValidationError("Faqat rasm fayllari qabul qilinadi (png, jpg, webp, svg)")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValidationError("Rasm hajmi 10 MB dan oshmasligi kerak")
    row = await store_bytes(session, unit_id=unit_id, data=data, mime=mime, filename=filename, created_by=created_by, is_public=True)
    return row, image_size(data, mime)


async def get_file(session: AsyncSession, file_id: uuid.UUID) -> StoredFile | None:
    return await session.get(StoredFile, file_id)


async def patient_owns_file(session: AsyncSession, file_id: uuid.UUID, patient_ids: Sequence[uuid.UUID]) -> bool:
    """Does an alive result document of one of these patients reference the file (its PDF)?"""
    if not patient_ids:
        return False
    stmt = (
        select(ResultDocument.id)
        .join(Order, Order.id == ResultDocument.order_id)
        .where(ResultDocument.pdf_file_id == file_id, alive(ResultDocument), alive(Order), Order.patient_id.in_(patient_ids))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


def content_disposition(filename: str | None) -> str | None:
    """Safe `Content-Disposition` value for a stored file.

    HTTP headers must be latin-1; a raw user-supplied name (e.g. Cyrillic) would crash the
    response with `UnicodeEncodeError`. `filename*=` (RFC 5987) carries the original name,
    `filename=` is the ASCII (transliterated) fallback for clients without RFC 5987 support.
    """
    if not filename:
        return None
    return f"inline; filename=\"{slugify(filename)}\"; filename*=UTF-8''{quote(filename)}"


async def load_bytes(session: AsyncSession, file_id: uuid.UUID | str | None) -> tuple[bytes, str] | None:
    if not file_id:
        return None
    row = await session.get(StoredFile, uuid.UUID(str(file_id)))
    return (bytes(row.data), row.mime) if row and row.data is not None else None
