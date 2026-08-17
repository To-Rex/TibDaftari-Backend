"""Template DTOs — mirror of Clinic-Web `src/domain/template.ts` (ResultTemplate, TemplateAsset)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from app.core.schemas import CamelModel

TemplateStatus = Literal["draft", "active", "archived"]
TemplateScope = Literal["item", "order"]
TemplateLanguage = Literal["uz", "ru", "en"]
AssetKind = Literal["logo", "stamp", "signature", "image"]

PAPERS = {"A4", "A5", "Letter"}
ORIENTATIONS = {"portrait", "landscape"}
ELEMENT_TYPES = {"text", "rect", "line", "image", "ellipse", "table", "field"}


def empty_doc() -> dict[str, Any]:
    """Frontend `emptyDoc()`: A4 portrait, white, margin 40, no elements."""
    return {"paper": "A4", "orientation": "portrait", "background": "#ffffff", "margin": 40, "elements": []}


def validate_doc(doc: Any) -> dict[str, Any]:
    """Permissive TemplateDoc validation: paper/orientation/background/margin + elements with id,type,x,y,w,h.

    Element payloads are kept verbatim (the renderer is tolerant); only the structural shape is checked
    so the editor can round-trip any element property without a backend release.
    """
    if not isinstance(doc, dict):
        raise ValueError("doc must be an object")
    paper = doc.get("paper", "A4")
    orientation = doc.get("orientation", "portrait")
    background = doc.get("background", "#ffffff")
    margin = doc.get("margin", 40)
    elements = doc.get("elements", [])
    if paper not in PAPERS:
        raise ValueError("doc.paper must be A4 | A5 | Letter")
    if orientation not in ORIENTATIONS:
        raise ValueError("doc.orientation must be portrait | landscape")
    if not isinstance(background, str) or len(background) > 40:
        raise ValueError("doc.background must be a colour string")
    if isinstance(margin, bool) or not isinstance(margin, int | float):
        raise ValueError("doc.margin must be a number")
    if not isinstance(elements, list):
        raise ValueError("doc.elements must be a list")
    for i, el in enumerate(elements):
        if not isinstance(el, dict):
            raise ValueError(f"doc.elements[{i}] must be an object")
        if not isinstance(el.get("id"), str) or not el["id"]:
            raise ValueError(f"doc.elements[{i}].id is required")
        if el.get("type") not in ELEMENT_TYPES:
            raise ValueError(f"doc.elements[{i}].type is invalid")
        for k in ("x", "y", "w", "h"):
            v = el.get(k)
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise ValueError(f"doc.elements[{i}].{k} must be a number")
    return {**doc, "paper": paper, "orientation": orientation, "background": background, "margin": margin, "elements": elements}


class TemplateQuery(CamelModel):
    status: TemplateStatus | None = None
    service_type_id: str | None = None
    search: str | None = Field(default=None, max_length=200)


class TemplateOut(CamelModel):
    id: str
    company_id: str
    name: str
    description: str | None = None
    status: TemplateStatus
    version: int
    service_type_ids: list[str]
    category_ids: list[str]
    scope: TemplateScope
    language: TemplateLanguage
    doc: dict[str, Any]
    thumbnail_url: str | None = None
    usage: int
    created_at: datetime
    updated_at: datetime


class _TemplateWrite(CamelModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    service_type_ids: list[str] | None = Field(default=None, max_length=500)
    category_ids: list[str] | None = Field(default=None, max_length=500)
    scope: TemplateScope | None = None
    language: TemplateLanguage | None = None
    doc: dict[str, Any] | None = None
    thumbnail_url: str | None = Field(default=None, max_length=2000)

    @field_validator("doc")
    @classmethod
    def _check_doc(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_doc(v) if v is not None else None


class TemplateCreateIn(_TemplateWrite):
    status: TemplateStatus | None = None


class TemplateUpdateIn(_TemplateWrite):
    """Partial update; `status` changes go through POST /templates/{id}/status."""


class TemplateStatusIn(CamelModel):
    status: TemplateStatus


class TemplatePreviewIn(CamelModel):
    doc: dict[str, Any] | None = None

    @field_validator("doc")
    @classmethod
    def _check_doc(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_doc(v) if v is not None else None


class TemplateAssetOut(CamelModel):
    id: str
    company_id: str
    kind: AssetKind
    name: str
    url: str
    width: float
    height: float
    employee_id: str | None = None


class TemplateAssetIn(CamelModel):
    kind: AssetKind
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=10, max_length=12_000_000)
    width: float = Field(default=0, ge=0)
    height: float = Field(default=0, ge=0)
    employee_id: str | None = None
