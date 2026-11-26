"""Per request: is the caller in training, may this write happen, and did a task just get done.

`trainee_of` is asked by `get_session` before the first query, so the whole request — the principal's own lookups
included — runs on the pool that fits the caller. It never raises for a bad or missing token (authentication is
`current_staff`'s job): anything that is not a valid staff token of an employee in training is simply "real".
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Request

from app.core.config import settings
from app.core.exceptions import AppError, AuthError, ForbiddenError
from app.core.security import decode_token
from app.infrastructure.db.session import engine
from app.modules.training import sandbox, tasks

log = logging.getLogger("app.training")

BLOCKED = "O‘quv rejimida bu amalni bajarib bo‘lmaydi. Avval o‘quv topshiriqlarini yakunlang."
NOT_READY = "O‘quv muhiti hozircha tayyor emas. Birozdan so‘ng qayta urinib ko‘ring."
_UNSET = object()


def _staff_id(request: Request) -> uuid.UUID | None:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    try:
        claims = decode_token(auth[7:].strip())
        return uuid.UUID(str(claims["sub"])) if claims.get("act") == "staff" else None
    except (AuthError, ValueError, KeyError):
        return None


async def trainee_of(request: Request) -> uuid.UUID | None:
    """The employee id when the caller is an employee in training, else None (memoised on the request)."""
    known = getattr(request.state, "trainee", _UNSET)
    if known is not _UNSET:
        return known  # type: ignore[return-value]
    result: uuid.UUID | None = None
    employee_id = _staff_id(request)
    if employee_id is not None:
        from app.modules.training import service

        active = settings.training_enabled and await service.is_active(employee_id)
        # told to the client with every answer (`X-Training`, see main.py): the interface follows a change made
        # elsewhere — a manager let them through or sent them back — without asking
        request.state.training = active
        # signing in and out is real bookkeeping (sessions), never practice
        if active and not tasks.api_path(request.url.path).startswith("/auth/"):
            result = employee_id
    request.state.trainee = result
    if result is not None and not await sandbox.ensure_ready(engine):
        # never fall through to the real data: without the sandbox a trainee cannot work at all
        raise AppError(NOT_READY, code="training_unavailable", status=503)
    return result


async def guard(request: Request) -> None:
    """Router-wide: a trainee reads freely (they see the sandbox) and writes only what `tasks.ALLOWED_WRITES` lists."""
    trainee = await trainee_of(request)
    if trainee is None or request.method in tasks.SAFE_METHODS:
        return
    if not tasks.write_allowed(request.method, tasks.api_path(request.url.path)):
        raise ForbiddenError(BLOCKED, code="training_mode")


async def after_commit(request: Request, employee_id: uuid.UUID) -> None:
    """The request of a trainee went through and is committed: tick the task it stands for (never fails the request)."""
    key = tasks.task_done_by(request.method, tasks.api_path(request.url.path))
    if key is None:
        return
    try:
        from app.modules.training import service

        await service.mark_done(employee_id, key)
    except Exception:  # pragma: no cover — progress is re-derivable; the work itself is saved
        log.exception("could not record training task %s for %s", key, employee_id)
