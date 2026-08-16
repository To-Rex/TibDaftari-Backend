# TibDaftari Backend — architecture & conventions

FastAPI · SQLAlchemy 2 (async, asyncpg) · PostgreSQL 16+ · Redis · Pydantic v2 · Alembic · fpdf2 · python-telegram-bot.
Serves the React SPA in `../Clinic-Web` (see `src/data/repositories.ts` — every repository method maps to one endpoint).

## Layout (feature modules over a thin shared core)

```
app/
  main.py                 FastAPI factory, middleware, lifespan (workers, telegram bots)
  __main__.py             python -m app → alembic upgrade head + uvicorn on $PORT
  core/                   cross-cutting: config, exceptions, security (argon2/JWT), crypto (Fernet),
                          permissions catalogue, schemas (CamelModel/Page), pagination, textutil (fold, phones),
                          timeutil (Asia/Tashkent), audit, ids (uuid7), logging
  infrastructure/
    db/  base.py (Base + mixins), session.py (engine, get_session), models/*.py (ALL tables)
    redis/ client, cache, rate_limit, lock (lease)
  api/  deps.py (DbSession, Staff, Patient principals, guards), router.py (aggregates modules)
  modules/<name>/         ONE FEATURE = ONE MODULE
    router.py             HTTP only: parse → call service → return DTO. No business logic.
    schemas.py            Pydantic DTOs (CamelModel). Names mirror frontend domain types.
    service.py            business rules, transactions, side effects (outbox, audit, notifications)
    repository.py         SQL only (select/insert/update helpers), no HTTP, no rules
    (extra files allowed: dispatcher.py, renderer/, texts.py …)
  workers/runner.py       background loops (outbox dispatcher, maintenance) with Redis lease
migrations/               Alembic (async env). Never edit an applied migration; add a new one.
seed/                     reference data (uz regions), demo dataset (from the frontend mock), legacy assets
tests/                    pytest (unit + API tests against a real DB when TEST_DATABASE_URL is set)
```

Dependency direction: `router → service → repository → models`; modules may import other modules'
**services** (never routers) — e.g. orders → messaging (enqueue SMS), orders → templates (render PDF).

## Rules every module follows
1. **Tenant isolation.** Every query on a tenant table filters `company_id`. Routers call
   `staff.scope(company_id)` (superadmin passes) and `staff.require(<permission>)`. Never trust a
   `companyId` from the body — take it from the path/session.
2. **Nothing is deleted.** Use `deleted_at/deleted_by` (soft delete). Reads filter `alive(Model)`.
   "Delete" endpoints set `deleted_at` and audit the action.
3. **Audit** meaningful mutations with `core.audit.audit(...)` (create/update/delete/login/approve/pay…).
4. **DTOs are camelCase** (`CamelModel`); shapes match `Clinic-Web/src/domain/*.ts` exactly
   (field names, enums, optionality). Timestamps ISO-8601 UTC `…Z` (ms), ids as strings, money int.
5. **Errors** are raised as `core.exceptions.AppError` subclasses with the exact codes/messages in
   `docs/DOMAIN_RULES.md` §1. Routers never craft error responses.
6. **Transactions**: the request session commits at the end (`get_session`). Services do not commit;
   they may `await session.flush()` to get ids. Background jobs use `session_scope()`.
7. **Performance**: async everywhere; no N+1 (use joins / `IN` batches); paginate lists;
   cache read-mostly catalog with `infrastructure.redis.cache` (key `co:{companyId}:...`,
   invalidate on write); indexes exist for every list filter (see models).
8. **Security**: argon2id passwords, JWT + Redis allow-list sessions, per-IP and per-login rate limits,
   secrets encrypted at rest (Fernet), masked in responses, never logged; strict Pydantic validation
   (lengths, enums); no raw SQL with string interpolation.
9. **Big data**: UUIDv7 keys, composite indexes `(company_id, created_at)`, partial indexes for queues,
   monthly partitions for `audit_log`, trigram indexes over `fold_text()` for search, denormalised
   counters (order.progress, patient.stats) maintained transactionally.

## Endpoint map (prefix `/api/v1`)
```
auth:      POST /auth/staff/login  GET /auth/staff/me  POST /auth/patient/otp/request  POST /auth/patient/otp/verify
           GET /auth/patient/me  POST /auth/logout
tenant:    GET/POST /companies  GET/PUT /companies/{id}  GET/POST /companies/{id}/branches  PUT /branches/{id}
           POST /companies/{id}/sms/test  PUT /companies/{id}/telegram
staff:     GET/POST /companies/{cid}/employees  GET/PUT /employees/{id}  PUT /employees/{id}/overrides
           GET/POST /companies/{cid}/roles  PUT/DELETE /roles/{id}
patients:  GET/POST /companies/{cid}/patients  GET /companies/{cid}/patients/search  POST /companies/{cid}/patients/duplicates
           GET/PUT /patients/{id}  GET /regions  GET /districts?regionId
catalog:   GET/POST /companies/{cid}/categories  PUT/DELETE /categories/{id}
           GET/POST /companies/{cid}/service-types  GET/PUT/DELETE /service-types/{id}
           GET/POST /companies/{cid}/schemas  GET/PUT /schemas/{id}  POST /schemas/{id}/publish
templates: GET/POST /companies/{cid}/templates  GET/PUT/DELETE /templates/{id}  POST /templates/{id}/status
           POST /templates/{id}/duplicate  GET/POST /companies/{cid}/assets  POST /templates/{id}/preview.pdf
orders:    GET/POST /companies/{cid}/orders  GET /orders/{id}  POST /orders/{id}/items  DELETE /orders/{id}/items/{itemId}
           POST /orders/{id}/pay  POST /orders/{id}/cancel  POST /payments/{id}/refund
           GET /companies/{cid}/worklist  GET /items/{id}  PUT /items/{id}/values  POST /items/{id}/submit
           POST /items/{id}/approve  POST /items/{id}/reject  POST /orders/{id}/approve  GET /orders/{id}/scope-items
           GET /companies/{cid}/documents  GET /documents/{id}  GET /documents/{id}/pdf
messaging: GET /companies/{cid}/outbox  POST /companies/{cid}/messages/send  GET /notifications  POST /notifications/read
reports:   GET /companies/{cid}/reports/dashboard  GET /companies/{cid}/reports/breakdown
portal:    GET /portal/overview  GET /portal/orders/{id}  GET /portal/documents/{id}  GET /portal/documents/{id}/pdf
files:     GET /files/{id}  (public assets & PDFs by unguessable id; PDFs also via /d/{token})
telegram:  POST /telegram/webhook/{companyId}/{secret} (optional; polling is default)
health:    GET /health  GET /health/ready
```
