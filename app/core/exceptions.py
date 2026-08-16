"""Application error hierarchy and the single JSON error envelope.

Every error leaves the API as::

    {"error": {"code": "not_found", "message": "Bemor topilmadi", "details": {...}}}

Services raise these; routers never build error responses by hand.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    status: int = 400
    code: str = "bad_request"

    def __init__(self, message: str | None = None, *, code: str | None = None, status: int | None = None, details: Any = None):
        self.message = message or self.code.replace("_", " ")
        if code:
            self.code = code
        if status:
            self.status = status
        self.details = details
        super().__init__(self.message)


class ValidationError(AppError):
    status = 422
    code = "validation_error"


class AuthError(AppError):
    status = 401
    code = "auth_error"


class ForbiddenError(AppError):
    status = 403
    code = "forbidden"


class NotFoundError(AppError):
    status = 404
    code = "not_found"


class ConflictError(AppError):
    status = 409
    code = "conflict"


class StateError(AppError):
    """Operation not allowed in the entity's current state (e.g. paying a cancelled order)."""

    status = 409
    code = "invalid_state"


class RateLimitedError(AppError):
    status = 429
    code = "rate_limited"


class ExternalServiceError(AppError):
    status = 502
    code = "external_service_error"


def _envelope(code: str, message: str, details: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return {"error": body}


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        headers = {"Retry-After": str(exc.details.get("retryAfter"))} if isinstance(exc.details, dict) and exc.details.get("retryAfter") else None
        return JSONResponse(_envelope(exc.code, exc.message, exc.details), status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": [str(x) for x in e.get("loc", [])], "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse(_envelope("validation_error", "So‘rov ma’lumotlari noto‘g‘ri", errors), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "auth_error", 403: "forbidden", 404: "not_found", 405: "method_not_allowed", 413: "payload_too_large", 429: "rate_limited"}.get(exc.status_code, "http_error")
        return JSONResponse(_envelope(code, str(exc.detail)), status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:  # pragma: no cover - safety net
        import logging

        logging.getLogger("app").exception("unhandled error: %s", exc)
        return JSONResponse(_envelope("internal_error", "Ichki xatolik. Keyinroq urinib ko‘ring."), status_code=500)
