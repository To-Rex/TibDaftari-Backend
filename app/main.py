"""FastAPI application factory.

Middleware order (outermost first): request-id → security headers → body-size guard →
rate limit → CORS → GZip. Lifespan boots Redis, background workers and Telegram bots.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import settings
from app.core.exceptions import install_exception_handlers
from app.core.logging import configure_logging
from app.infrastructure.db.session import dispose_engine
from app.infrastructure.redis.client import close_redis, get_redis
from app.infrastructure.redis.rate_limit import hit

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    try:
        await get_redis().ping()
        log.info("redis: connected")
    except Exception as exc:  # pragma: no cover
        log.warning("redis unavailable (%s) — running degraded (no rate limit / cache)", exc)

    tasks: list[asyncio.Task] = []
    if settings.workers_enabled:
        from app.workers.runner import start_workers

        tasks.extend(await start_workers())
    if settings.telegram_enabled:
        from app.modules.telegram.manager import bot_manager

        await bot_manager.start()
    log.info("%s started (env=%s, port=%s)", settings.app_name, settings.app_env, settings.port)
    try:
        yield
    finally:
        if settings.telegram_enabled:
            from app.modules.telegram.manager import bot_manager

            await bot_manager.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await close_redis()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        default_response_class=JSONResponse,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )
    install_exception_handlers(app)

    @app.middleware("http")
    async def _request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        started = time.perf_counter()
        # body-size guard (Content-Length based; streaming bodies are bounded by the proxy)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > settings.max_request_body_bytes:
            return JSONResponse({"error": {"code": "payload_too_large", "message": "So‘rov hajmi juda katta"}}, status_code=413)
        # per-IP rate limit (auth endpoints have a stricter one inside the auth router)
        ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (request.client.host if request.client else "?")
        if request.url.path.startswith("/api/") and not request.url.path.endswith("/health"):
            count, _ = await hit(f"rl:ip:{ip}", 60)
            if count > settings.rate_limit_per_minute:
                return JSONResponse({"error": {"code": "rate_limited", "message": "Juda ko‘p so‘rov. Biroz kuting."}}, status_code=429, headers={"Retry-After": "60"})
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        duration = (time.perf_counter() - started) * 1000
        if duration > 1000:
            log.warning("slow request %s %s %.0fms", request.method, request.url.path, duration)
        return response

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Content-Disposition"],
        max_age=600,
    )
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": settings.app_name, "status": "ok", "docs": "/docs" if not settings.is_production else ""}

    return app


app = create_app()
