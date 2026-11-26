"""The sandbox schema: where a trainee's patients, cheques, results and payments live.

`training.<table>` exists for every operational table and shadows the real one on the training pool
(`search_path = training, public`). It is BUILT, not migrated: `ensure()` reads the live definition of each real
table and makes its twin — columns, defaults, checks, indexes (`LIKE … INCLUDING ALL`), the same triggers, the
foreign keys that stay inside the sandbox, and the reporting views bound to the twins. A fingerprint of those
definitions is kept beside it; when a later migration changes a real table the fingerprint no longer matches and
the sandbox is dropped and built again (its contents are practice data — nothing is lost that matters, and each
employee's progress is kept in `public.employee_training`).

Deliberately NOT in the sandbox: reference data (the tree, staff, roles, the catalogue, schemas, templates, SMS
settings) — a trainee reads the real ones and the gate refuses every write to them — and `stored_files`
(logos and stamps are needed to draw a document; a training document's PDF is simply never stored).
Foreign keys from a twin to a real table are not made either: practice rows must never pin a real row.
"""

from __future__ import annotations

import logging
import re
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.infrastructure.db.realm import SANDBOX_ROLE, SANDBOX_SCHEMA

log = logging.getLogger("app.training")

#: the operational tables a trainee writes — parents before children (foreign keys are added in this order)
SANDBOX_TABLES: tuple[str, ...] = (
    "patients",
    "orders",
    "order_items",
    "payments",
    "result_documents",
    "order_counters",
    "outbox_messages",
    "notifications",
    "audit_log",
)
#: bump when the way the sandbox is built changes, so existing ones are rebuilt
BUILDER_VERSION = 1
_LOCK = "SELECT pg_advisory_xact_lock(hashtext('training.sandbox'))"

_FINGERPRINT = """
WITH t AS (
  SELECT c.oid, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relname = ANY(CAST(:tables AS text[]))
), lines AS (
  SELECT 'col:' || t.relname || '.' || a.attname || ':' || format_type(a.atttypid, a.atttypmod) || ':' || a.attnotnull::text
         || ':' || coalesce(pg_get_expr(d.adbin, d.adrelid), '') || ':' || a.attgenerated::text || a.attidentity::text AS line
  FROM t JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
  UNION ALL SELECT 'idx:' || pg_get_indexdef(i.indexrelid) FROM t JOIN pg_index i ON i.indrelid = t.oid
  UNION ALL SELECT 'con:' || t.relname || ':' || c.conname || ':' || pg_get_constraintdef(c.oid) FROM t JOIN pg_constraint c ON c.conrelid = t.oid
  UNION ALL SELECT 'trg:' || pg_get_triggerdef(g.oid) FROM t JOIN pg_trigger g ON g.tgrelid = t.oid AND NOT g.tgisinternal
  UNION ALL SELECT 'view:' || c.relname || ':' || pg_get_viewdef(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'v'
)
SELECT md5(string_agg(line, E'\\n' ORDER BY line)) FROM lines
"""

_ready = False
_role_ready = False
_last_try = 0.0

# What the sandbox role may do: read everything real, write only the twins. Repeated at every start — a table a
# later migration adds to `public` must be readable too.
_GRANTS = (
    f"DO $do$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{SANDBOX_ROLE}') THEN CREATE ROLE {SANDBOX_ROLE} NOLOGIN; END IF; END $do$",
    f"GRANT {SANDBOX_ROLE} TO CURRENT_USER",
    f"GRANT USAGE ON SCHEMA public TO {SANDBOX_ROLE}",
    f"GRANT USAGE ON SCHEMA {SANDBOX_SCHEMA} TO {SANDBOX_ROLE}",
    f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {SANDBOX_ROLE}",
    f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {SANDBOX_ROLE}",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {SANDBOX_SCHEMA} TO {SANDBOX_ROLE}",
    f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {SANDBOX_SCHEMA} TO {SANDBOX_ROLE}",
    f"REVOKE INSERT, UPDATE, DELETE ON {SANDBOX_SCHEMA}.sandbox_meta FROM {SANDBOX_ROLE}",
)


async def _grant(conn: AsyncConnection) -> bool:
    """Make the sandbox role and give it its rights. False when this database user may not (then the sandbox works
    without the role — see `session._as_trainee`)."""
    global _role_ready
    try:
        async with conn.begin_nested():
            for stmt in _GRANTS:
                await conn.execute(text(stmt))
        _role_ready = True
        return True
    except Exception as exc:
        _role_ready = False
        log.warning("training sandbox: role %s not set up (%s)", SANDBOX_ROLE, str(exc).splitlines()[0][:200])
        return False


def is_ready() -> bool:
    return _ready


async def fingerprint(conn: AsyncConnection) -> str:
    digest = (await conn.execute(text(_FINGERPRINT), {"tables": list(SANDBOX_TABLES)})).scalar_one()
    return f"{BUILDER_VERSION}:{digest}"


async def _current(conn: AsyncConnection) -> str | None:
    if not (await conn.execute(text(f"SELECT to_regclass('{SANDBOX_SCHEMA}.sandbox_meta') IS NOT NULL"))).scalar_one():
        return None
    return (await conn.execute(text(f"SELECT fingerprint FROM {SANDBOX_SCHEMA}.sandbox_meta LIMIT 1"))).scalar_one_or_none()


async def _build(conn: AsyncConnection, fp: str) -> None:
    s = SANDBOX_SCHEMA
    tables = list(SANDBOX_TABLES)
    # what the twins need, read while only `public` is on the path (definitions come out with unqualified names)
    present = set((await conn.execute(text("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = ANY(CAST(:t AS text[]))"), {"t": tables})).scalars())
    missing = [t for t in tables if t not in present]
    if missing:
        raise RuntimeError(f"sandbox tables missing in public: {missing}")
    triggers = (await conn.execute(text(
        "SELECT c.relname, pg_get_triggerdef(g.oid) FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT g.tgisinternal AND c.relname = ANY(CAST(:t AS text[])) ORDER BY c.relname, g.tgname"
    ), {"t": tables})).all()
    fkeys = (await conn.execute(text(
        "SELECT c.relname, k.conname, r.relname AS target, pg_get_constraintdef(k.oid) FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_class r ON r.oid = k.confrelid "
        "WHERE k.contype = 'f' AND n.nspname = 'public' AND c.relname = ANY(CAST(:t AS text[])) ORDER BY c.relname, k.conname"
    ), {"t": tables})).all()
    serials = (await conn.execute(text(
        "SELECT c.relname, a.attname FROM pg_attrdef d JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum JOIN pg_class c ON c.oid = d.adrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = ANY(CAST(:t AS text[])) AND pg_get_expr(d.adbin, d.adrelid) LIKE 'nextval(%'"
    ), {"t": tables})).all()
    views = (await conn.execute(text(
        "SELECT c.relname, pg_get_viewdef(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'v' ORDER BY c.oid"
    ))).all()

    await conn.execute(text(f"DROP SCHEMA IF EXISTS {s} CASCADE"))
    await conn.execute(text(f"CREATE SCHEMA {s}"))
    await conn.execute(text(f"COMMENT ON SCHEMA {s} IS 'O''quv rejimi: amaliyot uchun jadvallar nusxasi. Backend o''zi quradi va yangilaydi — qo''lda o''zgartirmang.'"))
    for t in tables:
        await conn.execute(text(f'CREATE TABLE {s}."{t}" (LIKE public."{t}" INCLUDING ALL)'))
    # a counter of its own for every serial column (the real sequence must not move because of practice rows)
    for t, col in serials:
        seq = f"{t}_{col}_seq"
        await conn.execute(text(f'CREATE SEQUENCE {s}."{seq}" OWNED BY {s}."{t}"."{col}"'))
        await conn.execute(text(f"ALTER TABLE {s}.\"{t}\" ALTER COLUMN \"{col}\" SET DEFAULT nextval('{s}.{seq}')"))
    for t, definition in triggers:
        await conn.execute(text(definition.replace(f" ON public.{t} ", f" ON {s}.{t} ", 1)))
    for t, name, target, definition in fkeys:
        if target not in present:
            continue  # points at real reference data: a practice row must not hold on to it
        ddl = re.sub(r"REFERENCES\s+(?:public\.)?\"?" + re.escape(target) + r"\"?\s*\(", f"REFERENCES {s}.{target}(", definition, count=1)
        await conn.execute(text(f'ALTER TABLE {s}."{t}" ADD CONSTRAINT "{name}" {ddl}'))
    # the reporting views once more, now reading the twins (names inside resolve in the sandbox first)
    await conn.execute(text(f"SET LOCAL search_path = {s}, public"))
    for name, definition in views:
        await conn.execute(text(f'CREATE VIEW {s}."{name}" AS {definition}'))
    await conn.execute(text(f"CREATE TABLE {s}.sandbox_meta (fingerprint text NOT NULL, built_at timestamptz NOT NULL DEFAULT now())"))
    await conn.execute(text(f"INSERT INTO {s}.sandbox_meta (fingerprint) VALUES (:fp)"), {"fp": fp})


async def ensure(engine: AsyncEngine) -> bool:
    """Make sure the sandbox matches the real tables; build or rebuild it when it does not. True when it was (re)built.
    Safe to call from several processes at once (one builds, the others find it done)."""
    global _ready, _last_try
    _last_try = time.monotonic()
    async with engine.begin() as conn:
        await conn.execute(text(_LOCK))
        fp = await fingerprint(conn)
        built = await _current(conn) != fp
        if built:
            await _build(conn, fp)
        await _grant(conn)
    _ready = True
    if not built:
        return False
    log.info("training sandbox built (%s tables, fingerprint %s)", len(SANDBOX_TABLES), fp)
    return True


def state() -> str:
    """For the readiness probe: `ready` (the sandbox is built and its connections cannot write real tables),
    `ready-norole` (built, but the database role could not be set up — only the API gate and the search path
    protect the real data), `unavailable` (not built: employees in training cannot work)."""
    if not _ready:
        return "unavailable"
    return "ready" if _role_ready else "ready-norole"


async def ensure_ready(engine: AsyncEngine, *, retry_after: float = 60.0) -> bool:
    """`is_ready()`, trying once more when an earlier attempt failed (not more often than every `retry_after` s)."""
    if _ready:
        return True
    if time.monotonic() - _last_try < retry_after and _last_try:
        return False
    try:
        await ensure(engine)
    except Exception:
        log.exception("training sandbox could not be built")
    return _ready
