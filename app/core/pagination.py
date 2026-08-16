"""Server-side pagination helpers matching the frontend `paginate()` semantics."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.schemas import Page, PageQuery


def total_pages(total: int, page_size: int) -> int:
    return max(1, math.ceil(total / page_size)) if page_size else 1


def page_of(items: Sequence[Any], q: PageQuery, total: int) -> Page[Any]:
    return Page(items=list(items), page=q.page, page_size=q.page_size, total=total, total_pages=total_pages(total, q.page_size))


async def paginate_query(session: AsyncSession, stmt: Select, q: PageQuery, *, order_by: Any = None, scalars: bool = True) -> tuple[list[Any], int]:
    """Runs COUNT(*) over the filtered statement + the page SELECT."""
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await session.execute(count_stmt)).scalar_one()
    page_stmt = stmt
    if order_by is not None:
        page_stmt = page_stmt.order_by(*order_by) if isinstance(order_by, list | tuple) else page_stmt.order_by(order_by)
    page_stmt = page_stmt.offset((q.page - 1) * q.page_size).limit(q.page_size)
    result = await session.execute(page_stmt)
    rows = result.scalars().all() if scalars else result.all()
    return list(rows), total


def sort_clause(sort_by: str | None, sort_dir: str | None, allowed: dict[str, Any], default: str, default_dir: str = "desc") -> Any:
    """Map camelCase sortBy → column with NULLS LAST; unknown fields fall back to default."""
    col = allowed.get(sort_by or "") if sort_by else None
    if col is None:
        col = allowed[default]
        direction = (sort_dir or default_dir).lower()
    else:
        direction = (sort_dir or default_dir).lower()
    return col.asc().nulls_last() if direction == "asc" else col.desc().nulls_last()


def paginate_list(items: Sequence[Any], q: PageQuery) -> Page[Any]:
    total = len(items)
    start = (q.page - 1) * q.page_size
    return Page(items=list(items[start : start + q.page_size]), page=q.page, page_size=q.page_size, total=total, total_pages=total_pages(total, q.page_size))
