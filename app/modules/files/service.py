"""Binary storage in Postgres (`stored_files`) — assets (logo/stamp/signature) and generated PDFs.

Files are content-addressed per company (sha256) so re-uploading the same image reuses the row.
Public URL: `/api/v1/files/{id}` (ids are UUIDv7 → unguessable enough for logos; PDFs additionally
go through document tokens in the orders module).
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import uuid
from urllib.parse import unquote_to_bytes

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.infrastructure.db.models import StoredFile

_DATA_URL = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?P<params>(;[\w-]+=[\w-]+)*)(?P<b64>;base64)?,(?P<data>.*)$", re.S)
ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/bmp", "image/svg+xml"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


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
    company_id: uuid.UUID | None,
    data: bytes,
    mime: str,
    filename: str | None = None,
    created_by: uuid.UUID | None = None,
    is_public: bool = False,
) -> StoredFile:
    sha = hashlib.sha256(data).hexdigest()
    # Dedupe by hash without pulling the existing blob into memory (only the id is needed).
    existing_id = (
        await session.execute(select(StoredFile.id).where(StoredFile.company_id == company_id, StoredFile.sha256 == sha).limit(1))
    ).scalar_one_or_none()
    if existing_id:
        existing = await session.get(StoredFile, existing_id, options=[defer(StoredFile.data)])
        if existing:
            return existing
    row = StoredFile(company_id=company_id, sha256=sha, mime=mime, size=len(data), filename=filename, data=data, is_public=is_public, created_by=created_by)
    session.add(row)
    await session.flush()
    return row


async def store_data_url(session: AsyncSession, *, company_id: uuid.UUID | None, data_url: str, filename: str | None = None, created_by: uuid.UUID | None = None) -> tuple[StoredFile, tuple[float, float] | None]:
    data, mime = decode_data_url(data_url)
    if mime not in ALLOWED_IMAGE_MIMES:
        raise ValidationError("Faqat rasm fayllari qabul qilinadi (png, jpg, webp, svg)")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValidationError("Rasm hajmi 8 MB dan oshmasligi kerak")
    row = await store_bytes(session, company_id=company_id, data=data, mime=mime, filename=filename, created_by=created_by, is_public=True)
    return row, image_size(data, mime)


async def get_file(session: AsyncSession, file_id: uuid.UUID) -> StoredFile | None:
    return await session.get(StoredFile, file_id)


async def load_bytes(session: AsyncSession, file_id: uuid.UUID | str | None) -> tuple[bytes, str] | None:
    if not file_id:
        return None
    row = await session.get(StoredFile, uuid.UUID(str(file_id)))
    return (bytes(row.data), row.mime) if row else None
