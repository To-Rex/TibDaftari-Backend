# TibDaftari Backend — architecture & conventions

FastAPI · SQLAlchemy 2 (async, asyncpg) · PostgreSQL 16+ · Redis · Pydantic v2 · Alembic · fpdf2 · python-telegram-bot.
Serves the React SPA in `../Clinic-Web` (see `src/data/repositories.ts` — every repository method maps to one endpoint).

## Layout (feature modules over a thin shared core)

```
app/
  main.py                 FastAPI factory, middleware, lifespan (workers, telegram bots)
  __main__.py             python -m app → alembic upgrade head + uvicorn on $PORT
  cli.py                  operations CLI (seed-reference, create-country-root, create-superadmin, …)
  core/                   cross-cutting: config, exceptions, security (argon2/JWT), crypto (Fernet),
                          permissions catalogue, scope (UnitScope — the organisation-tree scope of a request),
                          schemas (CamelModel/Page), pagination, textutil (fold, phones),
                          timeutil (Asia/Tashkent), audit, ids (uuid7), logging
  infrastructure/
    db/  base.py (Base + mixins), session.py (engine, get_session), snapshot.py (ORM row ↔ cache JSON),
         models/*.py (ALL tables — the ORM mirror of db/v2/*.sql)
    redis/ client (REDIS_URL=fake:// → in-process fakeredis for tests), cache, rate_limit, lock (lease)
  api/  deps.py (DbSession, Staff, Patient principals, Scope, guards), router.py (aggregates modules)
  modules/<name>/         ONE FEATURE = ONE MODULE
    router.py             HTTP only: parse → call service → return DTO. No business logic.
    schemas.py            Pydantic DTOs (CamelModel). Names mirror frontend domain types.
    service.py            business rules, transactions, side effects (outbox, audit, notifications)
    repository.py         SQL only (select/insert/update helpers), no HTTP, no rules
    (extra files allowed: dispatcher.py, renderer/, texts.py, reset.py …)
  workers/runner.py       background loops (outbox dispatcher, maintenance) with Redis lease
db/v2/                    THE database: NN_*.sql DDL + seeds (+ one-off legacy data migration 29_/30_, run.sh, migrate.sh)
migrations/               Alembic (async env). One baseline revision `v2_0001` applies db/v2/*.sql; add new revisions on top.
seed/                     reference data (countries, uz regions/districts), legacy assets
tests/                    pytest (unit + API tests against a throwaway database — never production)
docs/                     DOMAIN_RULES.md (behaviour), RENDERER_SPEC.md (PDF), V2_PORT.md (company/branch → org tree contract)
```

Modules: `auth`, `assistant` (the AI assistant: tools over the services, confirmed writes, per-unit model accounts), `units` (organisation tree, inherited SMS / texts / Telegram settings, superadmin reset, quick start), `staff`,
`patients` (+ geo reference), `catalog`, `templates` (+ renderer), `orders`, `messaging`, `reports`, `platform`
(territory activity view), `portal`, `telegram`, `files`, `health`.

Dependency direction: `router → service → repository → models`; modules may import other modules'
**services** (never routers) — e.g. orders → messaging (enqueue SMS), orders → templates (render PDF),
everything → units (`get_unit_or_404`, `facility_or_422`, `org_name`, `effective_settings`). The names every
module provides / may rely on are listed in `docs/V2_PORT.md` ("Inter-module API").

## Tenancy: the organisation tree

There are no companies and no branches. **One table, `org_units`**, holds the whole hierarchy —
`level` ∈ `country` → `region` → `district` → `facility` (Davlat → Viloyat → Tuman/Shahar → Muassasa(lar); any number
of facilities per district). `org_unit_closure (ancestor_id, descendant_id, depth)` is maintained by DB triggers, so
"everything under X" is one indexed `IN (...)` and never a recursive query.

* **Operational rows** (orders, items, payments, documents, outbox, patient registrations) point at a **facility**
  (`unit_id`; a trigger rejects any other level). **Catalogue rows** (categories, services, schemas, templates, assets)
  are **owned** by a unit of any level (`owner_unit_id`) and are visible to its whole subtree. **Settings**
  (`org_units.settings` jsonb), **messaging accounts** (SMS / Telegram) and **SMS texts** attach to any unit and are
  inherited downwards — the nearest level wins (`effective_settings(unit)`, `resolve_messaging_account(unit, channel)`,
  `resolve_sms_template(unit, key, lang)`). Patients form one registry **per country** (`country_id`,
  `registered_unit_id` = the registering facility). Cheque numbers come from `next_order_number(facility)` →
  `<facility code>-000123` (`order_counters`).
* **Staff scope**: an employee has a home `unit_id` (any level; NULL only for a superadmin) plus optional
  `employee_units`; their effective scope is the union of those subtrees (`StaffPrincipal.allowed_unit_ids`,
  `None` for a superadmin). Roles are system-wide (`scope_unit_id NULL`) or scoped to a unit's subtree. The
  permission key says WHAT, the unit says WHERE (`app/core/permissions.py`).
* **Every tenant URL is `/units/{unit_id}/…`** — the unit the client works in. The `Scope` dependency
  (`app/api/deps.py`) turns the path `{unit_id}` into a **`UnitScope`** (`app/core/scope.py`: `unit`, `descendant_ids`,
  `ancestor_ids`, `lineage_ids`, `facility_ids`, `is_facility`, `country_id`, `narrow()`) after
  `staff.require_unit(unit_id)` (403) — 404 "Birlik topilmadi" when the unit does not exist. An optional `unitId`
  query narrows a list to a sub-unit's subtree.
* **Repository rule of thumb**: operational tables → `Model.unit_id.in_(scope.descendant_ids)`; catalogue managed
  from a unit (admin pages) → `owner_unit_id.in_(scope.lineage_ids)`; catalogue usable at a facility (reception, lab) →
  `owner_unit_id.in_(scope.ancestor_ids)`; a row fetched by id → load it, then `staff.require_unit(row.unit_id)`
  (catalogue rows: the owner must be visible to the caller). Writes that need a facility take it from the path
  unit when it is one, else from the body (`unitId`), and reject non-facilities with 422 `facility_required`.
* **Caches** (Redis): `unit:{id}:scope` (UnitScope, 300 s), `unit:{id}:dto` (unit card), `unit:{id}:reports:*`,
  `principal:{employee_id}` (permissions + allowed unit set), `catalog:{unit_id}:*` (categories / services, 5 min),
  `tpl:*` (templates active at a unit, 120 s), `roles:{unit_id}`, `platform:regions:*` (60 s). Any org-tree write
  calls `units.service.invalidate_tree_caches()` (drops `unit:*`, `principal:*`, `catalog:*`, `tpl:*`, `roles:*`);
  catalogue / template / role writes drop their own namespace.

## Database

The schema is defined by **`db/v2/*.sql`** — one source of truth for the database, the app and every instance:
`00_foundation` (extensions, `fold_text`, `schema_migrations`), `01_reference` (countries / regions / districts),
`02_org` (tree, closure, triggers, `effective_settings`, `next_order_number`, `stored_files`), `03_staff`, `04_patients`,
`05_catalog` (+ `service_unit_settings`, `template_units`), `06_orders`, `07_messaging` (`messaging_accounts`,
`sms_templates`, outbox, notifications, telegram links), `08_audit` (monthly partitions), `09_views`
(`v_org_tree`, `v_facilities`, `v_territory_daily`), `10_messaging_env_bot`, seeds `20_` (geo), `21_` (system roles),
`31_` (permission renames). The Alembic baseline `v2_0001` applies exactly these scripts on an empty database and
records them in `schema_migrations`; a database already shaped by `db/v2/run.sh` is stamped (`alembic stamp v2_0001`).
`22_seed_org.sql` (the real organisation tree) and `29_`/`30_` (the one-off legacy data migration) are run by
hand, not by Alembic. Schema changes go in as new Alembic revisions **and** as new `db/v2/NN_*.sql` scripts.

ORM models (`app/infrastructure/db/models/`): `geo` (Country, Region, District) · `org` (OrgUnit, OrgUnitClosure,
OrderCounter) · `staff` (Employee, EmployeeUnit, Role, Session, OtpChallenge) · `patient` (Patient, TelegramLink,
TelegramChatPref) · `catalog` (Category, ServiceType, ServiceUnitSetting, AttributeSchema, ResultTemplate,
TemplateUnit, TemplateAsset, StoredFile) · `order` (Order, OrderItem, Payment, ResultDocument) · `messaging`
(MessagingAccount, SmsTemplate, OutboxMessage, Notification, AuditLog).

## Rules every module follows
1. **Scope isolation.** Every query on an operational table filters `unit_id` by the request's `UnitScope`
   (`scope.descendant_ids`), catalogue tables by `owner_unit_id` against the scope's lineage / ancestors.
   Routers take `scope: Scope` (access already checked) and call `staff.require(<permission>)`. Never trust a
   `unitId` from the body without checking `scope.contains()` / `staff.require_unit()`.
2. **Nothing is deleted.** Use `deleted_at/deleted_by` (soft delete). Reads filter `alive(Model)`.
   "Delete" endpoints set `deleted_at` and audit the action. (The only exception is the superadmin reset,
   which hard-deletes operational rows of one subtree on purpose — `units/reset.py`.)
3. **Audit** meaningful mutations with `core.audit.audit(..., unit_id=, country_id=)` (create/update/delete/login/
   approve/pay…) — pass the facility, or the administered unit.
4. **DTOs are camelCase** (`CamelModel`); shapes match `Clinic-Web/src/domain/*.ts` exactly
   (field names, enums, optionality). Timestamps ISO-8601 UTC `…Z` (ms), ids as strings, money int.
5. **Errors** are raised as `core.exceptions.AppError` subclasses with the exact codes/messages in
   `docs/DOMAIN_RULES.md` §1. Routers never craft error responses.
6. **Transactions**: the request session commits at the end (`get_session`). Services do not commit;
   they may `await session.flush()` to get ids. Background jobs use `session_scope()`.
7. **Performance**: async everywhere; no N+1 (use joins / `IN` batches); paginate lists;
   cache read-mostly data with `infrastructure.redis.cache` (keys above, invalidate on write);
   indexes exist for every list filter (see `db/v2/*.sql`).
8. **Security**: argon2id passwords, JWT + Redis allow-list sessions, per-IP and per-login rate limits,
   secrets encrypted at rest (Fernet), masked in responses, never logged; strict Pydantic validation
   (lengths, enums); no raw SQL with string interpolation.
9. **Big data**: UUIDv7 keys, composite indexes `(unit_id, created_at)`, partial indexes for queues,
   monthly partitions for `audit_log`, trigram indexes over `fold_text()` for search, denormalised
   counters (order.progress, patient.stats) maintained transactionally.

## Endpoint map (prefix `/api/v1`)
```
auth:      POST /auth/staff/login  GET /auth/staff/me  POST /auth/patient/otp/request  POST /auth/patient/otp/verify
           POST /auth/patient/login  POST /auth/patient/password  GET /auth/patient/me  POST /auth/logout
units:     GET /units/tree  POST /units  GET/PUT /units/{id}  GET/PUT /units/{id}/sms  POST /units/{id}/sms/test
           GET/PUT /units/{id}/sms-templates  PUT /units/{id}/telegram  GET /units/{id}/telegram/status
           GET/POST /units/{id}/reset  (superadmin)
           GET/POST /units/{id}/quick-start  (a facility ready to work in one request: nodes, staff, the sample's catalogue + templates, cheque — quickstart.py)
           GET/PUT /units/{id}/assistant · POST /units/{id}/assistant/test  (the unit's AI account, inherited downwards)
assistant  GET /assistant/status · GET/POST /assistant/threads · GET/DELETE /assistant/threads/{id} · POST /assistant/threads/{id}/messages
           POST /assistant/actions/{id}/confirm | cancel  (writes the model proposed run only after the employee's yes)
staff:     GET/POST /units/{uid}/employees  GET/PUT /employees/{id}  PUT /employees/{id}/overrides
           GET/POST /units/{uid}/roles  PUT/DELETE /roles/{id}
patients:  GET/POST /units/{uid}/patients  GET /units/{uid}/patients/search  POST /units/{uid}/patients/duplicates
           GET/PUT /patients/{id}  PUT/DELETE /patients/{id}/portal-access
           GET /countries  GET /regions?countryId  GET /districts?regionId
catalog:   GET/POST /units/{uid}/categories  PUT/DELETE /categories/{id}
           GET/POST /units/{uid}/service-types  GET/PUT/DELETE /service-types/{id}
           GET/POST /units/{uid}/schemas  GET/PUT /schemas/{id}  POST /schemas/{id}/publish
templates: GET/POST /units/{uid}/templates  POST /units/{uid}/templates/default-receipt  GET/PUT/DELETE /templates/{id}
           POST /templates/{id}/status  POST /templates/{id}/duplicate  POST /templates/{id}/preview.pdf
           GET/POST /units/{uid}/assets  DELETE /units/{uid}/assets/{assetId}
orders:    GET/POST /units/{uid}/orders  GET /units/{uid}/orders/summary  GET /units/{uid}/orders/export.xlsx
           GET /orders/{id}  GET /orders/{id}/receipt.pdf  POST /orders/{id}/items  DELETE /orders/{id}/items/{itemId}
           POST /orders/{id}/pay  POST /orders/{id}/cancel  DELETE /orders/{id}
           POST /payments/{id}/refund  PUT /payments/{id}/method
           GET /units/{uid}/worklist  GET /units/{uid}/worklist/counts  GET /items/{id}  PUT /items/{id}/values
           POST /items/{id}/submit  POST /items/{id}/approve  POST /items/{id}/revoke  POST /items/{id}/reject
           POST /orders/{id}/approve  GET /orders/{id}/scope-items
           GET /units/{uid}/documents  GET /documents/{id}  GET /documents/{id}/pdf  POST /documents/{id}/sms
           POST /documents/{id}/printed
           public: GET /d/{token}  POST /d/{token}/open  POST /d/{token}/otp/request  POST /d/{token}/otp/verify  GET /v/{token}
messaging: GET /units/{uid}/outbox  GET /units/{uid}/outbox/counts  POST /units/{uid}/messages/send
           GET /notifications  POST /notifications/read
reports:   GET /units/{uid}/reports/dashboard  GET /units/{uid}/reports/breakdown  GET /units/{uid}/reports/export.xlsx
           GET /units/{uid}/reports/{patients,results,results/list,services}
platform:  GET /platform/regions  (activity by country / region / district / facility; superadmin or a unitId in scope)
portal:    GET /portal/overview  GET /portal/orders/{id}  GET /portal/documents/{id}  GET /portal/documents/{id}/pdf
files:     GET /files/{id}  (public assets & PDFs by unguessable id; PDFs also via /d/{token})
telegram:  POST /telegram/webhook/{accountId}/{secret} (optional; polling is default)
health:    GET /health  GET /health/ready
```
