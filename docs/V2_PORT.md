# V2 port — from companies/branches to the organisation tree

TemoDB v2 has **no company and no branch**. One table, `org_units`, holds the whole state hierarchy:

```
country  (O‘zbekiston Respublikasi)            level = 'country'
└─ region   (Xorazm viloyati)                  level = 'region'
   └─ district (Bog‘ot tumani / Urganch shahri) level = 'district'
      └─ facility (Bog‘ot tumani SES, …)        level = 'facility'   ← ANY number per district
```

Cheques, payments, documents, outbox rows, patients' registration all point at a **facility** (`unit_id`).
Catalogue rows (categories, services, schemas, templates, assets) are **owned** by a unit at any level
(`owner_unit_id`) and are visible to the owner's whole subtree. Settings, SMS accounts and SMS texts attach to
any unit and are inherited downwards — the nearest level wins. `org_unit_closure (ancestor_id, descendant_id,
depth)` is kept by DB triggers, so "everything under X" is one indexed `IN (...)`.

The SQL that defines the database is in `db/v2/*.sql` (applied by `alembic upgrade head` on an empty DB).

## Old → new, table by table

| production (v1)                               | TemoDB v2                                                                   |
|-----------------------------------------------|-----------------------------------------------------------------------------|
| `companies`                                   | gone — a company's profile/settings live on its node (`org_units`)          |
| `branches`                                    | `org_units` rows with `level='facility'` (same ids), cheque prefix = `code` |
| `*.company_id`                                | gone; scope = `unit_id IN (descendants of the selected unit)`               |
| `*.branch_id` (orders, items, payments, outbox, docs) | `unit_id` (facility)                                                |
| `patients.company_id` / `branch_id`           | `country_id` (identity scope) / `registered_unit_id`                        |
| `employees.company_id`, `branch_ids[]`        | `unit_id` (home unit, any level; NULL only for superadmin) + `employee_units` (extra units) |
| `roles.company_id`                            | `scope_unit_id` (NULL = system role, available everywhere)                  |
| catalogue `company_id`                        | `owner_unit_id`                                                             |
| `service_types.branch_prices` / `disabled_branch_ids` | `service_unit_settings (service_type_id, unit_id, is_enabled, price)` — applies to the unit's subtree, nearest wins |
| `result_templates.branch_ids`                 | `template_units (template_id, unit_id)` — empty = owner's whole subtree     |
| company/branch `sms_*` columns, `telegram_bot_*` | `messaging_accounts (unit_id, channel sms\|telegram, provider, api_key_enc, …)`; `resolve_messaging_account(unit, channel)` |
| `settings.smsTemplates`                       | `sms_templates (unit_id, key, lang, body)`; `resolve_sms_template(unit, key, lang)` |
| `settings.documentFreeViewDays` etc.          | `org_units.settings` jsonb; `effective_settings(unit)` merges root → leaf  |
| `branches.order_seq`                          | `order_counters` + `next_order_number(facility) → 'UR-000123'`              |
| `audit_log.company_id`                        | `unit_id` + `country_id`                                                    |
| `sessions.company_id`, `otp_challenges.company_id` | `unit_id` (nullable)                                                  |
| `stored_files.company_id`                     | `unit_id` (nullable), `storage` db\|external, `object_key`                  |
| `telegram_links.company_id`, `telegram_chat_prefs.company_id` | `account_id` → messaging_accounts (the bot)                 |
| `orders` (new)                                | `first_paid_at` is a real column (set by trigger on the first payment)       |

DB helper functions available from SQL: `unit_descendants(uuid)`, `unit_ancestors(uuid)`, `unit_in_scope(unit, scope)`,
`unit_path(uuid)`, `effective_settings(uuid)`, `effective_setting(uuid, key)`, `employee_scope(emp)`,
`employee_can_see(emp, unit)`, `service_price_for(service, unit)`, `service_enabled_for(service, unit)`,
`resolve_messaging_account(unit, channel)`, `resolve_sms_template(unit, key, lang)`, `next_order_number(facility)`.

## Scope model in the API

* Every "tenant" URL `/companies/{company_id}/X` becomes **`/units/{unit_id}/X`**. `unit_id` is the unit the
  client is working in — a facility for the staff app, the home unit (region / country) for an administrator.
  Lists return everything under that unit. An optional `unitId` query filter narrows further (a sub-unit).
* `Staff` principal (`app/api/deps.py`): `unit_ids` (home + extra roots), `allowed_unit_ids` (every descendant of
  those; `None` for superadmin), `can_see(unit_id)`, `require_unit(unit_id)` (403). `company_id`, `branch_scope`,
  `allows_branch`, `can_switch_branch` no longer exist.
* `Scope` dependency (`app/api/deps.py`): `scope: Scope` on a route with `{unit_id}` in its path gives a
  `UnitScope` (`unit`, `descendant_ids`, `ancestor_ids`, `lineage_ids`, `facility_ids`, `is_facility`, `country_id`)
  after checking `staff.require_unit`. 404 "Birlik topilmadi" when the unit is missing.
* Repository rule of thumb:
  * operational tables → `Model.unit_id.in_(scope.descendant_ids)`
  * catalogue for management (admin pages) → `Model.owner_unit_id.in_(scope.lineage_ids)` (ancestors + subtree)
  * catalogue for use at a facility (reception, lab) → `Model.owner_unit_id.in_(scope.ancestor_ids)`
  * a row fetched by id (`/orders/{id}`) → load it, then `staff.require_unit(row.unit_id)`
    (catalogue rows: `staff.can_see_any(ancestors(owner) ∪ descendants(owner))` → use `units.service.unit_visible`)
* Writes that need a facility (create order, register patient, send a message from a facility) take the facility
  from the path unit when it is one, else from the body (`unitId`), and reject non-facilities with 422
  `"Bu amal faqat muassasa darajasida bajariladi"` (code `facility_required`).
* Superadmin works everywhere; there is no "impersonate a company" any more — the superadmin just selects a unit.

## Permissions (renamed keys)

| v1                        | v2                     | meaning                                            |
|---------------------------|------------------------|----------------------------------------------------|
| `admin.company.read`      | `admin.unit.read`      | read the unit's profile / settings                 |
| `admin.company.write`     | `admin.unit.write`     | edit the unit's profile / settings                 |
| `admin.branch.write`      | `admin.unit.manage`    | create / edit units under it (tumanlar, muassasalar)|
| `platform.company.manage` | `platform.unit.manage` | the whole tree, every country (superadmin)         |

Everything else is unchanged (35 keys in total). Roles are system-wide (`scope_unit_id NULL`) or scoped to a unit.

## Wire naming (DTOs — the frontend mirrors these exactly)

* `companyId` is gone everywhere. `branchId` → `unitId` (always the facility) on Order, OrderItem, Payment,
  ResultDocument, OutboxMessage, Patient (`registeredUnitId`), CreateOrderIn (`unitId`), PatientUpsertIn
  (`registeredUnitId`). `branchName` → `unitName`.
* Catalogue DTOs carry `ownerUnitId`. ServiceType: `unitPrices: {unitId: price}`, `disabledUnitIds: [...]`, and —
  when listed for a unit — `effectivePrice`, `available`. ResultTemplate: `unitIds`.
* Employee: `unitId`, `unitIds` (extra), `unitName`, `unitLevel`. Role: `scopeUnitId`.
* Session (`/auth/staff/login`, `/auth/staff/me`): `employeeId, unitId, unitIds, unit {id, level, name, code,
  countryId, path}, facilityId, isSuperAdmin, roleKey, fullName, permissions, accessToken, expiresAt`.
* OrgUnit DTO (`app/modules/units/schemas.py: OrgUnitOut`) is the only "organisation" object.
* Queries: `branchId` → `unitId` (optional narrowing); `BreakdownBy 'branch'` → `'unit'`.
* Error texts stay Uzbek. "Kompaniya topilmadi" / "Filial topilmadi" → "Birlik topilmadi" / "Muassasa topilmadi".

## Messaging

* `messaging.service.sms_account_for(session, unit_id) -> SmsAccount | None` — the nearest active `sms` account
  (provider `none` = SMS switched off for that subtree). `sms_texts_for(session, unit_id) -> dict[key, body]`.
* Telegram: one `messaging_accounts` row per bot (`channel='telegram'`); `api_key_enc NULL` = the platform bot whose
  token comes from `settings.telegram_bot_token`. The bot manager is keyed by **account id**.
* Message texts: `{company}` placeholder → the facility's region node name… no: `{org}` = the unit's own name,
  `{unit}` = facility name; keep `{company}` as an alias of the *country/region organisation name* (the nearest
  non-facility ancestor's `short_name or name`) so old texts keep working.

## Caches (Redis keys)

`unit:{id}:scope` (UnitScope, 300s) · `unit:{id}:dto` · `principal:{employee_id}` (now includes the allowed set) ·
`unit:{id}:catalog:*` · `unit:{id}:templates:*` · `roles:{scope_unit_id|system}`. Any org-tree write calls
`units.service.invalidate_tree_caches()` (drops `unit:*` and `principal:*`).

## Inter-module API (the names every module must provide / may rely on)

**`app.core.scope`** — `UnitScope(unit, descendant_ids, ancestor_ids, facility_ids)` with `.id .country_id .is_facility
.lineage_ids .contains(id) .narrow(sub_unit_ids)`; `load_scope(session, unit_id)`, `descendant_ids(session, ids)`,
`ancestor_ids(session, id)`, `facility_ids_under(session, id)`, `invalidate_scope_cache()`, `ids_of(values)`.

**`app.api.deps`** — `Staff`, `Scope` (= `UnitScope` of the path `{unit_id}`, access already checked), `Meta`,
`DbSession`, `StaffPrincipal.{id,is_super_admin,role_key,home_unit_id,home_facility_id,unit_ids,allowed_unit_ids,
can_see(id),can_see_any(ids),require_unit(id),restrict(ids),has(*),require(*)}`, `invalidate_principal_cache()`.

**`app.core.audit.audit(session, *, actor_type, actor_id, action, entity, entity_id=None, unit_id=None, country_id=None,
before=None, after=None, ip=None, request_id=None)`** — `company_id` is gone; pass the facility / administered unit.

**`app.modules.units.service`** — `get_unit_or_404(session, unit_id)`, `scope_or_404(session, unit_id)`,
`facility_or_422(session, scope, requested=None) -> OrgUnit`, `unit_path(session, unit_id) -> str`,
`org_name(session, unit_id) -> str` (nearest non-facility ancestor's short name — the "organisation" in texts),
`effective_settings(session, unit_id) -> dict`, `unit_dto(session, unit)`, `invalidate_tree_caches(unit_id=None)`,
`document_free_view_days(settings)` (in `units.schemas`).
**`app.modules.units.repository`** — `get_unit`, `get_unit_any` (deleted too), `units_by_ids(session, ids) -> dict`,
`resolve_account(session, unit_id, channel) -> MessagingAccount | None`, `resolve_sms_texts(session, unit_id, lang='uz')
-> dict[key, body]`, `subtree_ids`, `ancestors_of(session, unit_ids)`, `paths(session, ids)`, `effective_settings`.

**`app.modules.messaging.service`** (all async unless noted):
`SmsAccount(key_enc, priority, unit_id)`; `sms_account_for(session, unit_id) -> SmsAccount | None` (nearest active
xabarchi account with a key; `provider='none'` on the way = None); `sms_texts_for(session, unit_id) -> dict`;
`render_text(texts, kind, **vars) -> str` (sync; vars: patient, order, service, org, unit, company=org, branch=unit,
amount, total, remaining, count, code, link, login, password); `payment_receipt_text(session, unit_id, order_number,
amount, patient_name="", total=None, remaining=None)`, `result_ready_text(session, unit_id, service_name, patient_name="",
order_number="", link="")`, `result_ready_order_text(session, unit_id, template_name, count, patient_name="",
order_number="", link="")`, `otp_text(session, unit_id, code)`, `portal_access_text(session, unit_id, login, password,
link)`; `enqueue(session, *, unit_id, channel, kind, to, text, patient_id=None, order_id=None, document_id=None,
scheduled_at=None, payload=None, created_by=None) -> OutboxMessage`; `enqueue_sms_if_configured(session, unit_id,
**kwargs) -> OutboxMessage | None`; `patient_home_unit(session, patient) -> uuid | None` (registered unit, else the
unit of the latest cheque); `notify(session, *, unit_id, employee_id, title, body, kind="info", link=None)`;
`count_queued(session, unit_ids) -> int`; `mark_document_delivery(session, document_id, channel, status, detail=None)`;
`list_outbox(session, scope, q, staff)`, `outbox_counts(session, scope, q, staff)`, `send(session, scope, staff, body, meta)`.

**`app.modules.catalog.service`** — `get_service_types_by_ids(session, ids) -> dict[id, ServiceType]`,
`get_category(session, category_id) -> Category | None`, `get_schemas_by_ids(session, ids) -> dict`,
`effective_for_unit(session, service_types, unit_id) -> dict[id, tuple[enabled: bool, price: int]]` (nearest
`service_unit_settings` on the unit's path; default (True, base price)), `list_service_types(session, scope, q)`,
`invalidate_catalog_cache(owner_unit_id)`, `get_service_type_or_404(session, id, staff)`.

**`app.modules.templates.service`** — `list_active_templates(session, unit_id) -> list[ResultTemplate]` (active, owner on
the unit's path, and either no `template_units` or one on the path), `get_template_or_404(session, template_id,
staff=None)`, `ensure_default_receipt(session, owner_unit_id, unit_ids, language, created_by)`,
`invalidate_template_cache(owner_unit_id)`, `template_out`, `schema_dict`. Renderer context keys: `org` (the
organisation: name, legalName, phone, email, address, logoUrl) and `unit` (the facility: name, code, phone, address,
timezone); `company` and `branch` stay as ALIASES of `org` / `unit` so existing template docs keep rendering.

**`app.modules.patients.service`** — `get_patient_or_404(session, patient_id, staff, *, unit_ids=None) -> Patient`
(403 when the patient is outside the caller's scope: registered or visited there), `patient_out(p, stats=None)`,
`list_patients(session, scope, q, staff)`, `search_patients`, `find_duplicates(session, scope, body)`,
`create_patient(session, scope, staff, body, meta)` (facility = `facility_or_422(scope, body.registered_unit_id)`;
identity checked per `scope.country_id`), `update_patient`.

**`app.modules.staff.service`** — `get_employee_or_404(session, employee_id, staff)`, `employee_out(e, units=None)`,
`list_employees(session, scope, q)`, `create_employee(session, scope, body, staff, meta)` (`unitId` default = the
path unit; must be inside `scope`), `list_roles(session, scope)` (system roles + roles scoped to the lineage).

**`app.modules.orders.service`** — every entry point takes `scope: UnitScope` (lists) or loads the row then calls
`staff.require_unit(row.unit_id)`; `create_order(session, scope, staff, body, meta)` writes the cheque in
`facility_or_422(scope, body.unit_id)` and numbers it with `SELECT next_order_number(:unit_id)`; prices/availability via
`catalog.service.effective_for_unit`; templates via `templates.service.list_active_templates(unit_id)`.

**`app.modules.telegram.manager.bot_manager`** — keyed by **account id**: `start()`, `stop()`, `reload_account(id)`,
`is_running(id)`, `application_for(id)`, `deliver(session, msg)`; token = account's decrypted key, or
`settings.telegram_bot_token` when the account has none.

## Tests

`tests/conftest.py` cleans fixture **units** (`code LIKE 'T%'`, names `T-…`) created during the run; tests build their
fixture tree with `tests/helpers.make_tree()` (country node → region → district → facility) under the seeded
O‘zbekiston country row. Tests run against a throwaway database (`temodb_test`), never production.
