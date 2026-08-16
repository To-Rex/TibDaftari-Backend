"""Base Pydantic models: camelCase wire names, ORM-friendly, ISO-Z timestamps."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

T = TypeVar("T")


def iso_z(dt: datetime) -> str:
    """ISO-8601 with milliseconds and 'Z' — exactly what the frontend produces/expects."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class CamelModel(BaseModel):
    """All API DTOs inherit from this: accepts camelCase or snake_case, emits camelCase."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
        extra="ignore",
        ser_json_timedelta="iso8601",
    )

    @field_serializer("*", when_used="json", check_fields=False)
    def _serialize_scalars(self, v: Any) -> Any:
        if isinstance(v, uuid.UUID):
            return str(v)
        if isinstance(v, datetime):
            return iso_z(v)
        if isinstance(v, date):
            return v.isoformat()
        return v


class Page(CamelModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int


class PageQuery(CamelModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)
    search: str | None = None
    sort_by: str | None = None
    sort_dir: str | None = Field(default=None, pattern="^(asc|desc)$")


class OkOut(CamelModel):
    ok: bool = True


def dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(by_alias=True, mode="json")
