"""Async engine + session factory (asyncpg).

* One engine per process, pool sized from settings.
* `get_session` is the FastAPI dependency: one session per request, commit on
  success, rollback on error. Services never call `commit()` themselves except
  in explicit multi-step flows (`session.begin_nested()` for savepoints).
* `session_scope()` is for background workers / CLI.
* Both replay cache invalidations collected during the transaction once more after COMMIT
  (see `app.infrastructure.redis.cache`), so concurrent readers cannot pin stale data.
* A second, small pool (`training_engine`) serves employees who are still in training: its connections carry
  `search_path = training, public`, so the operational tables they touch are the sandbox copies (see
  `app.infrastructure.db.realm`). `get_session` picks the pool per request; workers and the CLI always get the
  real one unless they ask for the sandbox explicitly (`session_scope(training=True)`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import orjson
from fastapi import Request
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.infrastructure.db import realm
from app.infrastructure.redis import cache

log = logging.getLogger("app.db")


def _json_serializer(obj: object) -> str:
    return orjson.dumps(obj).decode()


def _json_deserializer(raw: str | bytes) -> object:
    return orjson.loads(raw)


engine: AsyncEngine = create_async_engine(
    settings.sqlalchemy_url,
    echo=settings.db_echo,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    pool_recycle=1800,
    json_serializer=_json_serializer,
    json_deserializer=_json_deserializer,
    connect_args={"server_settings": {"application_name": "tibdaftari-api", "timezone": "UTC"}},
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

# The training sandbox: the same database, but every connection resolves unqualified names in `training` first.
# A pool of its own, so a sandbox connection can never be handed to a real request (nor the other way round).
training_engine: AsyncEngine = create_async_engine(
    settings.sqlalchemy_url,
    echo=settings.db_echo,
    pool_size=2,
    max_overflow=8,
    pool_pre_ping=True,
    pool_recycle=1800,
    json_serializer=_json_serializer,
    json_deserializer=_json_deserializer,
    connect_args={"server_settings": {"application_name": "tibdaftari-training", "timezone": "UTC", "search_path": f"{realm.SANDBOX_SCHEMA},public"}},
)

TrainingSessionLocal = async_sessionmaker(training_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


@event.listens_for(training_engine.sync_engine, "connect")
def _as_trainee(dbapi_connection, _record) -> None:
    """Every sandbox connection gives up the right to write real tables (`SET ROLE`, see `realm.SANDBOX_ROLE`; the role
    is made by `training.sandbox.ensure`). Without the role — a database user that may not create one — the sandbox
    still works through its search_path and the API gate; only this last line of defence is missing."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"SET ROLE {realm.SANDBOX_ROLE}")
        dbapi_connection.commit()
    except Exception as exc:
        dbapi_connection.rollback()
        log.warning("training pool: role %s not taken (%s)", realm.SANDBOX_ROLE, str(exc).splitlines()[0][:160])
    finally:
        cursor.close()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    # who is asking decides which data the whole request sees: an employee in training gets the sandbox
    from app.modules.training import gate

    trainee = await gate.trainee_of(request)
    realm_token = realm.enter(trainee is not None)
    token = cache.begin_deferred()
    try:
        async with (TrainingSessionLocal if trainee is not None else SessionLocal)() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            await cache.run_deferred()
            if trainee is not None:
                # the request went through: a training task it stands for is done
                await gate.after_commit(request, trainee)
    finally:
        cache.end_deferred(token)
        realm.leave(realm_token)


@asynccontextmanager
async def session_scope(*, training: bool = False) -> AsyncIterator[AsyncSession]:
    """A session for workers / CLI — the real data, or the sandbox when `training` is asked for."""
    realm_token = realm.enter(training)
    token = cache.begin_deferred()
    try:
        async with (TrainingSessionLocal if training else SessionLocal)() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            await cache.run_deferred()
    finally:
        cache.end_deferred(token)
        realm.leave(realm_token)


async def dispose_engine() -> None:
    await engine.dispose()
    await training_engine.dispose()
