"""API v1 router — aggregates every module router. Prefix `/api/v1` is applied in main.py."""

from __future__ import annotations

from fastapi import APIRouter

from app.modules.auth.router import router as auth_router
from app.modules.catalog.router import router as catalog_router
from app.modules.files.router import router as files_router
from app.modules.health.router import router as health_router
from app.modules.messaging.router import router as messaging_router
from app.modules.orders.router import router as orders_router
from app.modules.patients.router import router as patients_router
from app.modules.portal.router import router as portal_router
from app.modules.reports.router import router as reports_router
from app.modules.staff.router import router as staff_router
from app.modules.telegram.router import router as telegram_router
from app.modules.templates.router import router as templates_router
from app.modules.tenant.router import router as tenant_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(tenant_router, tags=["tenant"])
api_router.include_router(staff_router, tags=["staff"])
api_router.include_router(patients_router, tags=["patients"])
api_router.include_router(catalog_router, tags=["catalog"])
api_router.include_router(templates_router, tags=["templates"])
api_router.include_router(orders_router, tags=["orders"])
api_router.include_router(messaging_router, tags=["messaging"])
api_router.include_router(reports_router, tags=["reports"])
api_router.include_router(portal_router, prefix="/portal", tags=["portal"])
api_router.include_router(files_router, tags=["files"])
api_router.include_router(telegram_router, tags=["telegram"])
