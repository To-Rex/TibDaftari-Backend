"""Catalog endpoints: categories, service types, attribute schemas (see ARCHITECTURE.md endpoint map)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import DbSession, Meta, Staff
from app.modules.catalog import service
from app.modules.catalog.schemas import (
    AttributeSchemaOut,
    CategoryCreateIn,
    CategoryOut,
    CategoryUpdateIn,
    SchemaCreateIn,
    SchemaUpdateIn,
    ServiceTypeCreateIn,
    ServiceTypeOut,
    ServiceTypeQuery,
    ServiceTypeUpdateIn,
)

router = APIRouter()

CATALOG_WRITE = "admin.catalog.write"
SCHEMA_WRITE = "admin.schema.write"

# ----------------------------------------------------------------------------- categories


@router.get("/companies/{company_id}/categories", response_model=list[CategoryOut], summary="Category tree of a company (any staff)")
async def list_categories(company_id: uuid.UUID, staff: Staff, session: DbSession) -> list[CategoryOut]:
    staff.scope(company_id)
    return await service.list_categories(session, company_id)


@router.post("/companies/{company_id}/categories", response_model=CategoryOut, status_code=201, summary="Create a category")
async def create_category(company_id: uuid.UUID, body: CategoryCreateIn, staff: Staff, session: DbSession, meta: Meta) -> CategoryOut:
    staff.require(CATALOG_WRITE).scope(company_id)
    return await service.create_category(session, company_id, body, staff, meta)


@router.put("/categories/{category_id}", response_model=CategoryOut, summary="Update a category (partial)")
async def update_category(category_id: uuid.UUID, body: CategoryUpdateIn, staff: Staff, session: DbSession, meta: Meta) -> CategoryOut:
    staff.require(CATALOG_WRITE)
    return await service.update_category(session, category_id, body, staff, meta)


@router.delete("/categories/{category_id}", status_code=204, summary="Soft-delete a category (no children, no services)")
async def delete_category(category_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> Response:
    staff.require(CATALOG_WRITE)
    await service.delete_category(session, category_id, staff, meta)
    return Response(status_code=204)


# ----------------------------------------------------------------------------- service types


@router.get("/companies/{company_id}/service-types", response_model=list[ServiceTypeOut], summary="Service types (category subtree, search, activeOnly) with 30-day stats")
async def list_service_types(company_id: uuid.UUID, q: Annotated[ServiceTypeQuery, Query()], staff: Staff, session: DbSession) -> list[ServiceTypeOut]:
    staff.scope(company_id)
    return await service.list_service_types(session, company_id, q)


@router.get("/service-types/{service_type_id}", response_model=ServiceTypeOut, summary="Service type details")
async def get_service_type(service_type_id: uuid.UUID, staff: Staff, session: DbSession) -> ServiceTypeOut:
    return await service.get_service_type_dto(session, service_type_id, staff)


@router.post("/companies/{company_id}/service-types", response_model=ServiceTypeOut, status_code=201, summary="Create a service type")
async def create_service_type(company_id: uuid.UUID, body: ServiceTypeCreateIn, staff: Staff, session: DbSession, meta: Meta) -> ServiceTypeOut:
    staff.require(CATALOG_WRITE).scope(company_id)
    return await service.create_service_type(session, company_id, body, staff, meta)


@router.put("/service-types/{service_type_id}", response_model=ServiceTypeOut, summary="Update a service type (partial; branchPrices wholesale)")
async def update_service_type(service_type_id: uuid.UUID, body: ServiceTypeUpdateIn, staff: Staff, session: DbSession, meta: Meta) -> ServiceTypeOut:
    staff.require(CATALOG_WRITE)
    return await service.update_service_type(session, service_type_id, body, staff, meta)


@router.delete("/service-types/{service_type_id}", status_code=204, summary="Soft-delete a service type (no order items)")
async def delete_service_type(service_type_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> Response:
    staff.require(CATALOG_WRITE)
    await service.delete_service_type(session, service_type_id, staff, meta)
    return Response(status_code=204)


# ----------------------------------------------------------------------------- attribute schemas


@router.get("/companies/{company_id}/schemas", response_model=list[AttributeSchemaOut], summary="Attribute schemas of a company (with usedBy)")
async def list_schemas(company_id: uuid.UUID, staff: Staff, session: DbSession) -> list[AttributeSchemaOut]:
    staff.scope(company_id)
    return await service.list_schemas(session, company_id)


@router.get("/schemas/{schema_id}", response_model=AttributeSchemaOut, summary="Attribute schema details")
async def get_schema(schema_id: uuid.UUID, staff: Staff, session: DbSession) -> AttributeSchemaOut:
    return await service.get_schema_dto(session, schema_id, staff)


@router.post("/companies/{company_id}/schemas", response_model=AttributeSchemaOut, status_code=201, summary="Create an attribute schema (draft, v1)")
async def create_schema(company_id: uuid.UUID, body: SchemaCreateIn, staff: Staff, session: DbSession, meta: Meta) -> AttributeSchemaOut:
    staff.require(SCHEMA_WRITE).scope(company_id)
    return await service.create_schema(session, company_id, body, staff, meta)


@router.put("/schemas/{schema_id}", response_model=AttributeSchemaOut, summary="Update an attribute schema (version bump when published and fields change)")
async def update_schema(schema_id: uuid.UUID, body: SchemaUpdateIn, staff: Staff, session: DbSession, meta: Meta) -> AttributeSchemaOut:
    staff.require(SCHEMA_WRITE)
    return await service.update_schema(session, schema_id, body, staff, meta)


@router.post("/schemas/{schema_id}/publish", response_model=AttributeSchemaOut, summary="Publish an attribute schema")
async def publish_schema(schema_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> AttributeSchemaOut:
    staff.require(SCHEMA_WRITE)
    return await service.publish_schema(session, schema_id, staff, meta)
