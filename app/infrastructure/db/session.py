"""Async engine + session factory (asyncpg).

* One engine per process, pool sized from settings.
* `get_session` is the FastAPI dependency: one session per request, commit on
  success, rollback on error. Services never call `commit()` themselves except
  in explicit multi-step flows (`session.begin_nested()` for savepoints).
* `session_scope()` is for background workers / CLI.
* Both replay cache invalidations collected during the transaction once more after COMMIT
  (see `app.infrastructure.redis.cache`), so concurrent readers cannot pin stale data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import orjson
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.infrastructure.redis import cache


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


async def get_session() -> AsyncIterator[AsyncSession]:
    token = cache.begin_deferred()
    try:
        async with SessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            await cache.run_deferred()
    finally:
        cache.end_deferred(token)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    token = cache.begin_deferred()
    try:
        async with SessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            await cache.run_deferred()
    finally:
        cache.end_deferred(token)


async def dispose_engine() -> None:
    await engine.dispose()
