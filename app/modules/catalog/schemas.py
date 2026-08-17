"""Catalog DTOs — mirror of Clinic-Web `src/domain/catalog.ts` (Category, ServiceType, AttributeSchema)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.schemas import CamelModel

WorkflowKind = Literal["lab", "consultation", "procedure", "inpatient", "pharmacy"]
DocumentScope = Literal["item", "order"]
SchemaStatus = Literal["draft", "published", "archived"]
FieldType = Literal["text", "longtext", "number", "select", "multiselect", "boolean", "date", "table"]

# ----------------------------------------------------------------------------- categories


class CategoryOut(CamelModel):
    id: str
    company_id: str
    parent_id: str | None
    name: str
    code: str | None = None
    icon: str | None = None
    color: str | None = None
    order: int
    is_active: bool
    phone: str | None = None
    workflow: WorkflowKind
    created_at: datetime
    updated_at: datetime


class CategoryCreateIn(CamelModel):
    parent_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=40)
    icon: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=20)
    order: int | None = Field(default=None, ge=0)
    is_active: bool = True
    phone: str | None = Field(default=None, max_length=40)
    workflow: WorkflowKind = "lab"


class CategoryUpdateIn(CamelModel):
    """Partial merge — only fields present in the payload are applied."""

    parent_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=40)
    icon: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=20)
    order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    phone: str | None = Field(default=None, max_length=40)
    workflow: WorkflowKind | None = None


# ----------------------------------------------------------------------------- service types


class ServiceStats(CamelModel):
    """`to_camel` would emit `ordered30D`; the frontend key is `ordered30d`."""

    ordered30d: int = Field(default=0, alias="ordered30d")


class ServiceTypeOut(CamelModel):
    id: str
    company_id: str
    category_id: str
    name: str
    code: str | None = None
    description: str | None = None
    price: int
    branch_prices: dict[str, int]
    turnaround_days: int
    order: int
    is_active: bool
    schema_id: str | None
    document_scope: DocumentScope
    default_template_id: str | None
    stats: ServiceStats | None = None
    created_at: datetime
    updated_at: datetime


class ServiceTypeCreateIn(CamelModel):
    category_id: str
    name: str = Field(min_length=1, max_length=300)
    code: str | None = Field(default=None, max_length=40)
    description: str | None = None
    price: int = Field(default=0, ge=0)
    branch_prices: dict[str, int] = Field(default_factory=dict)
    turnaround_days: int = Field(default=1, ge=0, le=365)
    order: int = Field(default=99, ge=0)
    is_active: bool = True
    schema_id: str | None = None
    document_scope: DocumentScope = "item"
    default_template_id: str | None = None


class ServiceTypeUpdateIn(CamelModel):
    """Partial merge; `branchPrices` is replaced wholesale when present."""

    category_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=300)
    code: str | None = Field(default=None, max_length=40)
    description: str | None = None
    price: int | None = Field(default=None, ge=0)
    branch_prices: dict[str, int] | None = None
    turnaround_days: int | None = Field(default=None, ge=0, le=365)
    order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    schema_id: str | None = None
    document_scope: DocumentScope | None = None
    default_template_id: str | None = None


class ServiceTypeQuery(CamelModel):
    category_id: str | None = None
    search: str | None = Field(default=None, max_length=200)
    active_only: bool = False


# ----------------------------------------------------------------------------- attribute schemas


class FieldDefIn(BaseModel):
    """Permissive field definition: the required core is validated, everything else passes through
    untouched (options, references, columns, presetRows, visibleIf …) so the frontend owns the shape."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=300)
    type: FieldType
    required: bool = False
    order: int = 0

    @field_validator("key")
    @classmethod
    def _key_no_spaces(cls, v: str) -> str:
        if any(ch.isspace() for ch in v):
            raise ValueError("kalitda bo‘sh joy bo‘lmasligi kerak")
        return v


class AttributeSchemaOut(CamelModel):
    id: str
    company_id: str
    name: str
    description: str | None = None
    version: int
    status: SchemaStatus
    fields: list[dict[str, Any]]
    used_by: int
    created_at: datetime
    updated_at: datetime


class SchemaCreateIn(CamelModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    fields: list[FieldDefIn] = Field(default_factory=list, max_length=500)


class SchemaUpdateIn(CamelModel):
    """Partial merge; `fields` replaced wholesale (bumps version on a published schema)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    fields: list[FieldDefIn] | None = Field(default=None, max_length=500)
