# Domain rules — behavioural contract (derived from the frontend mock repositories)

The frontend (Clinic-Web) was first built on `src/data/mock/repos/*.mock.ts`. The backend reproduces
that behaviour 1:1 (wire shape + rules) **except** where marked **FIX** (deliberate corrections) —
those are bugs in the mock that must not be reproduced. When in doubt, read the mock source.

**Tenancy (2026-11-09, v2):** there is no company and no branch any more. Every row hangs off the
organisation tree `org_units` — Davlat (country) → Viloyat (region) → Tuman/Shahar (district) →
Muassasa(lar) (facility, any number per district). Cheques, payments, documents, outbox rows and
patient registrations point at a **facility** (`unitId`); catalogue rows are **owned** by a unit of any level
(`ownerUnitId`) and visible to its whole subtree; settings, SMS accounts and SMS texts are inherited
downwards (nearest level wins). Every tenant URL is `/units/{unit_id}/…` — the unit the client works in
(a facility for the staff app, the home unit of an administrator); lists return everything under it and an
optional `unitId` query narrows to a sub-unit's subtree. The port contract is `docs/V2_PORT.md`.

Wire conventions: camelCase JSON, ids are strings (UUID), timestamps ISO-8601 UTC with `Z`, money = integer UZS.
Errors: `{"error": {"code", "message", "details?"}}` with the HTTP status below. Messages are Uzbek and shown
verbatim in the UI.

## 0. Shared primitives
* **Pagination**: `pageSize` default 20 (cap 200 in backend), `page` clamped to ≥1, past-the-end → empty items;
  `total` counted after filters; `totalPages = max(1, ceil(total/pageSize))` (**1 when total is 0**).
  Response `{ items, page, pageSize, total, totalPages }`.
* **Sorting**: `sortBy` (top-level field only; whitelist per endpoint), `sortDir` asc|desc; NULLs last in both directions.
* **Search folding** `fold(s)`: lower-case → Cyrillic→Latin translit (а→a б→b в→v г→g д→d е→e ё→yo ж→j з→z и→i й→y к→k л→l м→m н→n о→o п→p р→r с→s т→t у→u ф→f х→x ц→ts ч→ch ш→sh щ→sh ъ→'' ы→i ь→'' э→e ю→yu я→ya ў→o қ→q ғ→g ҳ→h) → strip apostrophes `‘’'\`ʻ` → replace every `h` with `x` → collapse whitespace, trim.
  `matches(hay, needle)`: empty needle → true; hay null → false; else fold(hay) contains fold(needle).
  Backend implementation: store nothing extra; apply the same folding in SQL via an IMMUTABLE `fold_text(text)` PL/pgSQL function + pg_trgm GIN index on `fold_text(col)` for patients/orders/units; or filter in Python for small sets (catalog).
* Create paths ignore any client-supplied `id`. Timestamps `createdAt/updatedAt` are server-owned.
* Money rounding: `round()` half-up.
* **Scope** (`app/core/scope.py`): the `{unit_id}` of a URL resolves to a `UnitScope` — the unit, `descendantIds`
  (itself + everything under it, deleted units included so their history stays), `ancestorIds` (root → itself),
  `facilityIds` (alive facilities under it). 403 when the unit is outside the caller's units, 404 "Birlik
  topilmadi" when it does not exist. Operational rows are filtered by `unitId ∈ descendants`; catalogue rows by
  `ownerUnitId ∈ ancestors` (usable AT the unit) or `∈ lineage` (ancestors + subtree: managed FROM the unit).
  A row addressed by id (`/orders/{id}`, `/patients/{id}`, …) is loaded first, then checked against the caller's
  allowed units (403; patients: 404, no existence leak).
* **Facility-only writes** (create a cheque, register a patient, send an SMS): the facility is the path unit when
  it is one, else the body's `unitId` / `registeredUnitId` (a facility under the path unit — 404 "Muassasa
  topilmadi" otherwise); a non-facility → 422 `facility_required` "Bu amal faqat muassasa darajasida bajariladi".

## 1. Error catalogue (status / code / message)
| status | code | message |
|---|---|---|
| 401 | auth_error | Hisob topilmadi · Login yoki parol noto‘g‘ri · Sessiya tugagan |
| 401 | otp_expired | Kod muddati tugagan |
| 401 | otp_invalid | Kod noto‘g‘ri |
| 403 | inactive | Hisob faol emas |
| 403 | forbidden | Ruxsat yo‘q · Faqat superadmin uchun · Bu kategoriya sizga biriktirilmagan |
| 403 | otp_required | Hujjatni ochish uchun SMS kod kerak |
| 403 | login_session_only | Parolni login bilan kirganda o‘zgartirish mumkin |
| 403 | payment_locked | To‘lovga 1 kundan ko‘p vaqt o‘tgan — uni faqat administrator yoki rahbar o‘zgartira oladi |
| 404 | not_found | Birlik topilmadi · Muassasa topilmadi · Xodim topilmadi · Rol topilmadi · Bemor topilmadi · Bu raqam bilan bemor topilmadi. Klinikaga murojaat qiling. · Namuna muassasa topilmadi · Kategoriya topilmadi · Xizmat topilmadi · Sxema topilmadi · Shablon topilmadi · Rasm topilmadi · Chek topilmadi · Tahlil topilmadi · To‘lov topilmadi · Hujjat topilmadi · Fayl topilmadi |
| 409 | conflict | Bu login band · Bu kod band  (a unit code or a service code already used) |
| 409 | login_taken | Bu login band  (patient portal login) |
| 409 | in_use | Bu rol xodimlarga biriktirilgan · Tizim rolini o‘chirib bo‘lmaydi · Kategoriyada xizmatlar bor · Bu xizmat bo‘yicha buyurtmalar bor — o‘chirish o‘rniga nofaol qiling |
| 409 | has_children | Avval ichki kategoriyalarni o‘chiring |
| 409 | active | Faol shablonni o‘chirib bo‘lmaydi — avval arxivlang |
| 409 | closed | Chek yopilgan |
| 409 | not_in_branch | «{xizmat}» xizmati bu muassasada ko‘rsatilmaydi  (the code keeps its v1 name — the UI maps it) |
| 409 | in_progress | Laboratoriya boshlagan tahlilni o‘chirib bo‘lmaydi |
| 409 | paid | To‘langan chekdan xizmat o‘chirib bo‘lmaydi · To‘langan chekni bekor qilish uchun avval qaytarish qiling |
| 409 | already_paid | Chek allaqachon to‘langan |
| 409 | approved | Tasdiqlangan natijani o‘zgartirib bo‘lmaydi |
| 409 | state | Avval natijalarni saqlang · Faqat yuborilgan natijani tasdiqlash mumkin · Tasdiqlash uchun yuborilgan tahlil yo‘q · Faqat yuborilgan natijani qaytarish mumkin · Qaytarilgan to‘lovni o‘zgartirib bo‘lmaydi |
| 409 | sms_not_configured / no_phone | public result link (§7): Bu muassasada SMS yuborish sozlanmagan — hujjatni havola orqali ochib bo‘lmaydi. … · Hujjatga telefon raqami bog‘lanmagan — SMS kod yuborib bo‘lmaydi. … |
| 409 | duplicate_passport / duplicate_pinfl / duplicate_phone | Bu passport raqami bilan bemor mavjud · Bu JSHSHIR bilan bemor mavjud · Bu telefon raqami bilan bemor mavjud |
| 410 | revoked | public result link of a revoked document |
| 422 | facility_required | Bu amal faqat muassasa darajasida bajariladi |
| 422 | leaf / kind_required / code_required | Muassasa ostida birlik yaratib bo‘lmaydi · Muassasa turi tanlanmagan · Muassasa kodi kiritilmagan |
| 422 | unit_required / ai_api_key_required / ai_base_url_required / ai_provider_required | Ish birligi tanlanmagan · AI provayder API kaliti kiritilmagan · Provayder manzili (base URL) kiritilmagan · AI provayder tanlanmagan  (assistant) |
| 409 | assistant_not_configured / action_resolved / action_expired | Yordamchi bu birlik uchun sozlanmagan … · Bu amal allaqachon yakunlangan · Tasdiqlash muddati o‘tdi — so‘rovni qayta yuboring |
| 429 | assistant_rate_limited | Yordamchiga juda ko‘p so‘rov yuborildi — birozdan keyin davom eting |
| 502 | ai_auth / ai_rate_limit / ai_model / ai_bad_request / ai_status / ai_connection | the model provider refused or was unreachable (message names the cause) |
| 422 | region_required / district_required | Viloyat tanlanmagan · Tuman / shahar tanlanmagan  (quick start from a country / region node) |
| 422 | validation_error | Davlat topilmadi · Viloyat topilmadi · Tuman topilmadi · Viloyat tanlangan davlatga tegishli emas · Tuman tanlangan viloyatga tegishli emas  (unit geo nesting) |
| 422 | unit_required | Xodim uchun birlik tanlanmagan |
| 422 | sms_not_configured / sms_api_key_required / telegram_invalid_token | SMS provayder sozlanmagan · Xabarchi API kaliti kiritilmagan · Telegram bot tokeni noto‘g‘ri  (unit settings pages) |
| 422 | invalid_phone | Telefon raqam noto‘g‘ri |
| 422 | invalid_login / invalid_password | Login 3–40 belgi: lotin harflari, raqamlar va . _ - (harf yoki raqam bilan boshlanadi) · Parol bo‘sh joy bilan boshlanmasin va tugamasin |
| 422 | invalid_permission | Noma’lum ruxsat kaliti: {keys} |
| 422 | parts / same_method | Qismlar yig‘indisi to‘lov summasiga teng bo‘lishi kerak · To‘lov usuli o‘zgarmadi  (payment method correction) |
| 422 | parts / confirm | Noma’lum bo‘lim: {parts} · Kamida bitta bo‘limni tanlang · Tasdiqlash so‘zi noto‘g‘ri  (superadmin reset) |
| 422 | wrong_password / same_password | Joriy parol noto‘g‘ri · Yangi parol joriysidan farq qilsin |
| 422 | empty | Kamida bitta maydon kerak · Bo‘sh shablonni faollashtirib bo‘lmaydi · Chekda xizmat yo‘q |
| 422 | amount | Summa 1 – {remaining} oralig‘ida bo‘lishi kerak  (raw integer, en dash) |
| 422 | required | To‘ldirilmagan: {labels joined ', '} |
| 422 | no_template | Bu xizmat uchun faol shablon yo‘q · Faol shablon topilmadi |
| 422 | scope | Bu shablon chek darajasidagi hujjat emas |
| 429 | rate_limited | Juda ko‘p urinish. Keyinroq urinib ko‘ring. |

## 2. Auth
* Permission catalogue (37 keys `<module>.<resource>.<action>`): reception.patient.read/write, reception.order.create/cancel/delete, reception.payment.create/refund/late_edit, lab.worklist.read, lab.result.write/submit, confirm.result.read/view/approve/resend, reports.finance.read, reports.finance.recent, reports.operations.read, reports.export, messaging.send/broadcast, **admin.unit.read/write/manage**, admin.employee.read/write, admin.role.write, admin.catalog.read/write, admin.schema.write, admin.template.read/write/publish, admin.settings.write, **admin.unit.reset**, **assistant.use**, **platform.unit.manage**.
  * `admin.unit.read` / `admin.unit.write` — read / edit the profile and settings of the unit (and the units
    under it); `admin.unit.manage` — create / edit units under it (tumanlar, muassasalar);
    `admin.unit.reset` — wipe a unit and everything under it ("like newborn"; superadmin and the installer, inside
    their scope — `GET/POST /units/{id}/reset`, 403 "Faqat superadmin yoki o‘rnatuvchi uchun" for everyone else);
    `platform.unit.manage` — the whole tree, every country (superadmin).
  * **WHERE a permission applies is not part of the key**: it is the employee's scope — the subtree(s) of their
    home unit and extra units. The same `admin.unit.write` edits one facility for a facility administrator and a
    whole viloyat for a regional one. A superadmin works everywhere and simply selects a unit (no
    "impersonate a company" any more).
  * v1 names are mapped at read time (`app/core/permissions.py RENAMED`) and rewritten in the data by
    `db/v2/31_permissions_rename.sql`: `admin.company.read → admin.unit.read`, `admin.company.write →
    admin.unit.write`, `admin.branch.write → admin.unit.manage`, `platform.company.manage → platform.unit.manage`.
* `confirm.result.view` (2026-10, registrar default): approved results only — the worklist with `status=approved`
  (any other status needs `lab.worklist.read`), items / orders / documents / the patient, print, and the SMS re-send
  (`POST /documents/{id}/sms` takes approve | messaging.send | confirm.result.resend). The system `registrator`
  role carries it together with `confirm.result.resend` (`db/v2/21_seed_roles.sql`).
* Registrar reports (2026-10-31): the `registrator` role has `reports.finance.read` + `reports.export` — the same
  reports as the admin (revenue without the 7-day cap, CSV / Excel export), still confined to the employee's
  units (scope subtree). `reports.finance.recent` stays on the role: if an admin takes `reports.finance.read`
  away, the 7-day cap applies again.
  Effective = role.permissions ∪ overrides.allow − overrides.deny (deny wins).
* System roles (`scopeUnitId` null — available everywhere; fixed ids in `21_seed_roles.sql` + `32_installer_role.sql`):
  `superadmin` (all 37), `installer` ("O‘rnatuvchi": all except platform.* — incl. `admin.unit.reset`), `admin` (all
  except platform.* and reset), `registrator`, `laborant`, `vrach`, `rahbar`.
* **O‘rnatuvchi (installer)** — a superadmin of one territory. Works like a superadmin inside the units a superadmin
  gave the employee and nowhere else: home unit = a **region** (their viloyat) or the **country** node (the whole
  country); extra units = other **regions of the same country**. Any other shape → 422 `installer_scope`. The role
  (and the superadmin role) is granted, re-scoped, edited, password-reset or deactivated by **superadmins only**
  (`PROTECTED_ROLE_KEYS`; 403 "Ruxsat yo‘q" for a unit administrator, also on `/overrides`); its key is reserved
  (422 `reserved`). A staff reset keeps installer accounts like it keeps superadmins. Installers never get
  `platform.*` (no other countries, no country roots, no Davlatlar tab); the session says `roleKey = "installer"`,
  `isSuperAdmin = false`.
* **No self-escalation** (2026-11-10): an employee edits their own name, contacts, login and password, never their own
  `roleId`, `overrides` (also `PUT /employees/{id}/overrides`), `unitId` / `unitIds`, `categoryIds`, `status` or the
  superadmin flag → 403 `self_edit` "O‘z rolingiz, ruxsatlaringiz va hududingizni o‘zingiz o‘zgartira olmaysiz"; the
  role one holds cannot be edited or deleted by its holder → 403 `own_role`. Superadmins are exempt. The session
  carries `roleId` so the UI can lock the matching controls. A role says WHAT; the employee's
  unit says WHERE — the same `rahbar` attached to a region node is a viloyat rahbari, on a facility a muassasa
  rahbari. Unit-scoped roles (`scopeUnitId` = a unit; usable in its subtree) are created under a unit (§3).
* **StaffSession** `{actor:'staff', employeeId, unitId, unitIds, unit {id, level, name, code, countryId, path}, facilityId, isSuperAdmin, roleKey, fullName, permissions[], accessToken, expiresAt}`; `unitId` = the home unit (any level; null for a superadmin, who picks any unit of the tree); `unitIds` = every scope root (home + extra units — the UI switches among these subtrees); `facilityId` = the home unit when it is a facility (where this employee's cheques are written); `isSuperAdmin = employee.is_super_admin` (**FIX**: not derived from a client-editable role key); `roleKey = role.key`; staff token TTL 12h (fixed - the JWT `exp` is immutable and the SPA keeps the login token; `staffMe` only records `lastSeenAt`).
* `staffLogin`: login trimmed + case-insensitive; password argon2 verify; wrong either → 401 auth_error "Login yoki parol noto‘g‘ri"; inactive → 403 inactive; on success set lastLoginAt; **FIX** rate limit + lockout (settings) → 429.
* `staffMe(token)`: rebuild session from DB each call (role changes apply immediately); revoked/expired → 401 "Sessiya tugagan". The principal (permissions + allowed unit set) is cached as `principal:{employeeId}` and dropped on every employee, role or org-tree write.
* Patient OTP: `requestPatientOtp(phone)`: digits; 9 digits → prefix 998; must match `^998\d{9}$` else 422 invalid_phone; find the patient by phone in the national registry (oldest by created_at) else 404 "Bu raqam bilan bemor topilmadi. Klinikaga murojaat qiling."; challenge TTL 5 min; code random N digits (settings.otp_length; **dev mode: returned as devCode**), delivered as SMS (kind otp) through the patient's home unit — `patient_home_unit`: the registering facility, else the facility of the latest cheque — when an SMS account is in effect there (§8); resend cooldown 60s per phone (429).
  `verifyPatientOtp(challengeId, code, phone)`: unknown/expired → 401 otp_expired; wrong → 401 otp_invalid (max 3 attempts then expired); success → consume, patient.portal.linked = true, session TTL 30 days.
* Patient portal login (2026-11-04) — an extra way in, phone + SMS code is unchanged:
  * Reception (`reception.patient.write`, unit-scoped like the patient update): `PUT /patients/{id}/portal-access`
    `{login?, password?, sendSms?, unitId?}` issues or renews it. Login: trimmed, lower-case, `^[a-z0-9][a-z0-9._-]{2,39}$`
    (422 invalid_login), unique among alive patients of the whole registry (one portal form; 409 login_taken); empty → the
    current one, else `<first word of the name in Latin><4 digits>`. Password: given (6–128, no outer spaces) or
    generated (8 chars, no look-alikes). Every call sets a new password, clears the lockout and ends the patient's
    login sessions. The answer `{login, password, sms, patient}` is the only time the password is shown (argon2 hash
    stored). `sendSms` → kind `portal_access` to the patient's phone through the SMS account in effect for the
    working facility (`unitId` when it lies inside the employee's scope, else the patient's home unit); the outbox
    shows the text with the password masked, the real text travels encrypted (as OTP); `sms` = queued |
    not_configured | invalid_phone.
    `DELETE /patients/{id}/portal-access` clears login + password and ends the login sessions (idempotent).
    Audited (`portal_access_set` / `portal_access_remove`, never the password). `patient.portal.login` / `loginSetAt`.
  * `POST /auth/patient/login {login, password}`: login case-insensitive; same rate limits and lockout as the staff
    login (settings.login_max_attempts / login_lock_minutes → 429); wrong either → 401 "Login yoki parol noto‘g‘ri".
    Success → portal.linked = true, session TTL 30 days, `via: 'login'` in the session and the token, `login`.
  * A login session acts for **its own patient only** (relatives sharing one phone each see their own results); an
    SMS-code session keeps acting for every patient of the phone. `sessions.method` = otp | login (NULL: older rows).
  * `POST /auth/patient/password {currentPassword, newPassword}` (login sessions only, else 403 login_session_only):
    wrong current → 422 wrong_password; same → 422 same_password; the other login sessions end, this one stays.
* `logout(token)`: revoke jti (Redis + sessions.revoked_at). Idempotent.

## 3. Organisation units / staff
* **The tree** (`db/v2/02_org.sql`): one table `org_units` for every level — `level` ∈ country | region | district |
  facility; `parentId` (null only for a country node); `countryId`, `regionId`, `districtId` (geo reference rows);
  `kind` (facility only: ses | clinic | laboratory | polyclinic | hospital | pharmacy | other); `code` (facility: the
  cheque prefix `UR-000123`; optional above); profile (`name`, `shortName`, `legalName`, `phone`, `email`, `address`,
  `timezone`, `logoUrl`), `isActive`, `sortOrder`, `settings` (inherited jsonb). `org_unit_closure (ancestor,
  descendant, depth)` is kept by DB triggers — "everything under X" is one indexed `IN (...)`; the level is
  immutable; a unit cannot be moved under its own descendant. OrgUnit DTO (`units/schemas.py: OrgUnitOut`) is the
  only "organisation" object: `… depth, path ("O‘zbekiston Respublikasi › Xorazm viloyati › Bog‘ot tumani › Bog‘ot
  tumani SES"), settings {documentFreeViewDays, locale, own}, sms?, telegram?, counts {regions, districts,
  facilities, employees}`.
* `GET /units/tree?countryId&rootId&includeInactive` — every unit the caller may see, flat with parentId / depth /
  path (light cards: no SMS / Telegram). Scoped staff get their subtree(s) plus the path above them (read-only
  breadcrumb context); superadmin everything, optionally one country or one subtree.
* `GET /units/{id}` — the full card (profile, effective settings, SMS / Telegram status, counts). Readable when the
  unit lies in the caller's scope or on the path above it, else 403; unknown → 404 "Birlik topilmadi". Cached
  `unit:{id}:dto`.
* `createUnit` (`POST /units`, `admin.unit.manage` | `platform.unit.manage`; the parent must be inside the caller's
  scope): **level follows from the parent** (country → region → district → facility; under a facility → 422 `leaf`
  "Muassasa ostida birlik yaratib bo‘lmaydi"). Without a parent a **country node** is created (`countryId`
  required, `platform.unit.manage`; one live node per country). **Geo nesting**: a region node needs `regionId`
  belonging to the parent's country, a district node `districtId` belonging to the parent's region (422 "Viloyat /
  Tuman topilmadi", "… tegishli emas"); one live node per region / district row. A **facility** inherits the
  district's geo and needs `kind` (422 `kind_required`) + `code` (422 `code_required`; upper-cased,
  `^[A-Za-z0-9][A-Za-z0-9-]{0,11}$`). **Codes are unique among live units** → 409 "Bu kod band". `timezone`
  defaults to the country's. Defaults: isActive true, sortOrder 0. Audited; every tree write drops `unit:*`,
  `principal:*`, `catalog:*`, `tpl:*`, `roles:*`.
* `updateUnit` (`PUT /units/{id}`): partial merge; level and parent are immutable; `code` change re-checks uniqueness
  (409 "Bu kod band"); `kind` only on facilities. Profile fields need `admin.unit.write`; a body that changes only
  `documentFreeViewDays` (0–90) / `locale` is allowed with `admin.settings.write` (the SMS settings page) and is
  written into the unit's own `settings`. There is no delete endpoint (the superadmin reset's `units` part
  soft-deletes child units).
* **Inherited settings**: `effective_settings(unit)` = shallow merge root → … → the unit itself, **nearest level
  wins** (`documentFreeViewDays`, `locale`, `telegramWebhookSecret`, …). The DTO shows the effective values and the
  unit's own overrides (`settings.own`).
* **SMS account per unit** (`GET/PUT /units/{id}/sms`; GET with admin.settings.write | admin.unit.write |
  admin.unit.read, PUT with admin.settings.write | admin.unit.write): rows of `messaging_accounts (unit_id, channel
  'sms', provider xabarchi|none, api_key_enc, priority, sender_note)`. `mode` = `inherited` (no account of its own —
  the nearest ancestor's is used), `own` (its own Xabarchi key; required on the first save → 422
  `sms_api_key_required`; stored Fernet-encrypted, returned masked `xab_live_••••••••XXXX`; `defaultPriority`
  urgent | transactional | bulk, `senderNote`), `off` (provider `none` — SMS switched off for this unit and
  everything under it). The answer names the account in effect (`provider`, `effectiveUnitId/Name`).
  `POST /units/{id}/sms/test {to?}` sends a real SMS with that account (422 `sms_not_configured` "SMS provayder
  sozlanmagan" when none; 422 invalid_phone).
* **SMS texts per unit** (`GET/PUT /units/{id}/sms-templates`, same permissions): `sms_templates (unit_id, key,
  lang, body)` with keys `payment_receipt`, `result_ready`, `reminder`. Resolution per key: the unit's own →
  the nearest ancestor's → the platform default (an empty value = default). Answer `{unitId, templates
  (effective), own, inherited, applied}`; `applyToAll` writes the same texts to every unit under it too (they stop
  inheriting; `applied` = units written). Texts: §10.
* **Telegram per unit** (`PUT /units/{id}/telegram {botToken}`, admin.settings.write | admin.unit.write): a
  `messaging_accounts` row with channel `telegram` on the unit — the token is checked against Telegram `getMe`
  (422 `telegram_invalid_token`), encrypted, and the bot is (re)started; `botToken` null disconnects it and the unit
  falls back to the inherited bot (or the platform bot, §12). `GET /units/{id}/telegram/status` (admin.settings.write)
  shows the bot in effect (own / inherited) and whether it runs on this instance.
* **Employees**: an employee has a **home unit** (`unitId`, any level; null only for a superadmin) and optional
  **extra units** (`unitIds`, `employee_units`); their effective scope = the union of those subtrees
  (`allowed_unit_ids`); a superadmin sees everything. DTO adds `unitName`, `unitLevel`.
* `listEmployees` (`GET /units/{id}/employees`): employees homed in the unit's subtree; filters `unitId` (a sub-unit →
  its subtree), roleId, status, search on fullName/login/phone(digits); default sort fullName asc. Superadmin
  employees (no unit) appear only for superadmin callers and never inside a `unitId` narrowing.
* `saveEmployee`: `password` handled separately (argon2); create (`POST /units/{id}/employees`): `unitId` defaults to
  the path unit; it and every `unitIds` entry must be alive and inside the request scope (404 "Birlik topilmadi");
  the role must be a system role or one scoped to a unit on the home unit's path (404 "Rol topilmadi"); the
  `superadmin` role / `isSuperAdmin` flag are set by superadmins only (such an employee has no unit at all);
  login unique (case-insensitive, global) → 409 conflict "Bu login band"; default password `123456` if none;
  avatarHue random 0..359 persisted. Update: replace unitIds/categoryIds/overrides wholesale; `unitId` moves the
  home unit (must stay inside the caller's scope; an employee without a unit → 422 `unit_required` "Xodim uchun
  birlik tanlanmagan" unless superadmin); login change allowed if still unique; password change or deactivation
  revokes the sessions; only a superadmin may modify a superadmin employee. Never return password data.
* `setOverrides`: replace `{allow, deny}` wholesale (validate keys ⊂ catalogue → 422 invalid_permission; `platform.*`
  keys only by superadmins).
* `listRoles` (`GET /units/{id}/roles`): system roles + roles scoped to the lineage (the path above + the subtree),
  each with `employeeCount` (employees under the requesting unit). Cached `roles:{unitId}`.
* `saveRole`: `POST /units/{id}/roles` creates a role **scoped to the path unit** (`scopeUnitId`; usable in its
  subtree); key = slug of the key or name (unique per scope, 409), reserved key `superadmin`; permissions ⊂
  catalogue, `platform.*` only by superadmins. **FIX** 404 on unknown id. System roles: name/description/permissions
  editable **by superadmins only**, key/isSystem never; a unit role is edited by callers who see its unit. Create
  defaults: permissions [], isSystem false.
* `deleteRole`: in use → 409 in_use; system role → 409 in_use "Tizim rolini o‘chirib bo‘lmaydi"; else soft delete.
* **Platform regional view (2026-11-09)** `GET /platform/regions?dateFrom&dateTo&countryId?&regionId?&districtId?&unitId?&groupBy?`:
  a superadmin (`platform.unit.manage`) sees every country; any other staff member must pass a `unitId` inside their
  own scope (a region administrator sees their region). Activity of the alive facilities in the territory and/or
  under the unit, folded by the **facility's own geo** (`country_id / region_id / district_id` — every facility has a
  district by construction; `v_territory_daily`). `groupBy` ∈ country | region | district | **unit** (= facility),
  default one level below the narrowest filter / root unit (nothing or country → region, region → district, district
  → unit). Per row `{id, kind, name, parentName, unitId (the tree node of the row when it exists), units (alive
  facilities), orders (non-cancelled, opened in range), revenue (Σ non-refunded payments taken in range), patientsNew
  (registered in range at a facility there), resultsReady (final documents issued in range), smsSent (sent in range
  from a facility there)}`; facility rows add `code`, `facilityKind`, `isActive`. Rows: revenue desc, orders desc,
  name; `total`; `trend` = daily cheques/revenue of the whole scope. No `unassigned` / `coverage` sections any
  more. Numbers only — no patient data. Range ≤ 3 years; Redis 60 s.
* **Superadmin reset** (`GET /units/{id}/reset` preview, `POST /units/{id}/reset {parts, confirm}`; superadmin only, else
  403 "Faqat superadmin uchun"): brings one unit back to the state right after it was created without touching
  anything outside its subtree — irreversible, previewed (counts per part), confirmed by typing the facility code
  (else the unit name; 422 `confirm`), audited with `unit_id` = the unit. The **parts depend on the level**;
  dependencies are added automatically (422 `parts` on unknown / empty):

  | level | part | removes | ⇒ requires |
  |---|---|---|---|
  | facility | orders | its cheques, services, payments, result documents (+ PDFs), outbox rows, notifications; cheque numbering restarts from 1 | |
  | facility | patients | patients of this facility only (registered here, no cheque elsewhere) + their portal / Telegram sessions, links, codes; patients who visit other facilities are re-registered at the facility of their first other cheque | orders |
  | facility | templates | templates owned by the facility or bound to it only (+ assets); shared templates just lose this facility | orders |
  | facility | staff | employees of this facility only (superadmins and the caller are kept; employees who also serve elsewhere move there); unused roles scoped to it | orders |
  | facility | prices | the facility's `service_unit_settings` overrides | |
  | country / region / district | orders | every cheque under the unit (+ services, payments, documents, PDFs, outbox, notifications); every facility's numbering restarts | |
  | … | patients | patients registered under the unit with no cheque outside it; the others are re-registered where their first outside cheque was written | orders |
  | … | templates | templates and assets owned inside the subtree; bindings of outside templates to its units are dropped | orders |
  | … | catalog | categories, services and result schemas owned inside the subtree | templates, orders |
  | … | staff | employees homed inside the subtree except superadmins and the caller; unused roles scoped inside it | orders |
  | … | units | the child units — soft-deleted, never the unit itself; their employees and roles move up to the unit, their own catalogue / templates / messaging settings go | orders, patients |

  The unit row, its profile and settings, the reference data and the audit log stay. Operational rows are
  hard-deleted in one transaction (cheque numbers are unique per facility, so restarting the numbering needs the
  old cheques really gone).

* **Quick start — «Tezkor ishga tushirish»** (`GET/POST /units/{id}/quick-start`, the path unit = a country, region or
  district node the caller manages: `admin.unit.manage` + `admin.employee.write`, 403 otherwise; under a facility →
  422 `leaf`). ONE request, one transaction, a facility ready to work:
  1. the **chain** — from a country node `regionId` + `districtId` (geo ids), from a region node `districtId`; the
     region / district nodes are reused when they exist, created otherwise (`regionName` / `districtName` override
     the geo names); missing geo → 422 `region_required` / `district_required`;
  2. the **facility** (`facility {name, kind, code, shortName?, legalName?, phone?, email?, address?, timezone?}` — the
     usual create rules: 409 "Bu kod band", 422 `kind_required`);
  3. the **sample** (`sample {facilityId, owner: region|district|facility}`, optional; needs `admin.catalog.write` +
     `admin.schema.write` + `admin.template.write`, the sample visible to the caller else 403, 404 "Namuna muassasa
     topilmadi"): catalogue rows the sample uses (owned on its path) that the new facility already inherits (owner on
     ITS path) are kept as they are; the others — categories (tree kept), services (code, price, turnaround, schema,
     default template remapped), published / draft schemas — are **copied and owned by the `owner` level** of the new
     facility (its region node by default); every non-archived template usable at the sample (any scope) is copied,
     its service / category ids remapped, and **bound to the new facility** (`template_units`), owned by `owner`;
  4. the **cheque** (`activateReceipt`, default true): an active receipt template usable at the facility stays; else
     the newest usable draft is activated; else the standard cheque is created active (owner `owner`, bound to the
     facility);
  5. the **staff** (`staff [{role, fullName, login}]`, roles registrator | vrach | laborant | rahbar | admin — system
     roles, protected ones never; `password` for all, default 123456): logins are lower-cased and validated
     (`^[a-z0-9][a-z0-9._-]{2,39}$`, 422 `invalid_login`); a login already used (by anyone, or earlier in the same
     request) gets a number — `kassa`, `kassa2`, `kassa3` …; each account is a plain `create_employee` homed at the
     facility.
  Audited once as `quick_start` on the facility (plus the per-row audits). Response: `{facility, region?, district?
  (the nodes CREATED), employees [{id, fullName, login, roleKey, roleName, password}], catalog {sampleName, ownerName,
  inherited, copied}, receipt {id, name, status}?}` — the clear-text password is returned once, for the credentials
  sheet. The preview (`GET …?regionId=&districtId=`) returns `{parent, regionNode?, districtNode? (existing nodes;
  null = would be created), inherits {categories, serviceTypes, schemas, templates, receipts} + inheritsFrom
  (owners above the facility-to-be; bound templates do not count), samples [{id, name, code, kind, path, counts,
  sameCatalogue}] (visible active facilities with something to copy, biggest first), roles [{key, id, name,
  suffix}], defaultPassword}`.

* **AI assistant — «Yordamchi»** (`app/modules/assistant`, permission `assistant.use` — every system role has it; what
  the assistant may DO is gated by the employee's other keys). A chat that acts inside the employee's own rights:
  * **Account** (`GET/PUT /units/{id}/assistant`, `POST …/assistant/test`; read with admin.settings.write | admin.unit.read,
    write with admin.settings.write; 403 outside the caller's units): `assistant_accounts` rows inherited downwards like
    messaging accounts — `mode` inherited | own | off; `provider` anthropic (official Anthropic SDK) | openai | deepseek |
    deploy | custom (OpenAI-compatible `baseUrl`, required for custom → 422 `ai_base_url_required`); `model` empty =
    the provider preset; the key is Fernet-encrypted and only its masked tail comes back; a first `own` save without a
    key → 422 `ai_api_key_required`. `settings {effort, maxTokens}` — `maxTokens` 0 (the default) = the model's own
    output maximum (Claude: the Models API cap, 128K on the 5.x line, replies streamed; OpenAI-compatible: the
    parameter is omitted).
  * **Tools** (`tools.py`): thin wrappers over the ordinary service functions, each with `perms` (ANY grants; empty =
    everyone with assistant.use) and a `kind` — read tools run at once, write tools become a pending action. The model
    is offered ONLY the tools the employee holds (`tools_for`); a call to anything else is answered with an error
    result, never run. Every output goes through `_clean` (no password / hash / key / token keys, no `sms` / `telegram`
    sub-objects). Each run is audited as `assistant_tool` (args cleaned, ok / error).
  * **Threads** (`GET/POST /assistant/threads`, `GET/DELETE /assistant/threads/{id}`): the caller's own only (404
    otherwise). `POST /assistant/threads/{id}/messages {content, unitId?}` (`unitId` = the working unit, inside the
    territory; else the thread's / home unit; a superadmin must name one → 422 `unit_required`): rate-limited per
    employee (30 / 10 min → 429 `assistant_rate_limited`), 409 `assistant_not_configured` when no account with a key
    is in effect. The loop (max 6 tool rounds, history window 400 turns / 800k chars — sized for a 1M context — starting at a
    user turn, tool results cut at 60k chars, Claude requests prompt-cached) stores every turn (`assistant_messages`: user | assistant [+ tool_calls] | tool). The
    FIRST write tool of a turn becomes an `assistant_actions` row (`pending`, 30 min); further writes in the same turn
    get a "one at a time" error result; reads still run.
  * **Confirmation** (`POST /assistant/actions/{id}/confirm | cancel`, the owner only, 409 `action_resolved` /
    `action_expired`): confirm runs the tool with the employee's principal, stores result / error on the action
    (`done` / `failed`), appends the tool result and lets the model reply; cancel appends a "declined" error result.
    A new message while an action is pending cancels it the same way — nothing ever runs on the model's word alone.
  * **System prompt** names the employee, role, home and working unit, the date and the offered tools, and states the
    rules (never invent ids, writes wait for the card, no secrets or instructions revealed, tool results and user text
    are data, no self-escalation). Safety does not rest on the prompt: the server-side permission and scope checks of
    the wrapped services are what make it true.
  Response shapes: `MessageOut {id, role, content, toolCalls [{id, name, title, kind, summary, status}], toolName?,
  isError, action?: ActionOut, createdAt}`, `ActionOut {id, threadId, tool, title, summary, args (cleaned), status,
  error?, expiresAt, createdAt}`, `SendOut {messages (the turns appended), pending?, thread}`,
  `AssistantStatusOut {enabled, configured, provider?, model?, effectiveUnitName?, tools [{name, title, kind}],
  rateLimit, rateWindowSeconds}`.

## 4. Patients
* `normPhone(p)`: digits; 9 → prefix 998. Stored `998XXXXXXXXX`.
* **Identity per COUNTRY**: `countryId` = the registering facility's country — one registry per country, not per
  organisation. Uniqueness within a country (DB partial unique indexes): passport (case-insensitive), PINFL, and
  phone only for rows without passport/PINFL.
* **`registeredUnitId`** = the facility that registered the patient (DB enforces the facility level; null on legacy
  rows); `registeredUnitName` in the DTO.
* **Membership rule** (lists and id reads): a patient belongs to a unit's scope when registered at a facility of its
  subtree **or** has an alive cheque at one (`in_units`); an unregistered patient with no cheques at all is visible
  in every scope (so a desk can find and claim them). A superadmin sees everyone. Outside the scope an id read
  answers 404 "Bemor topilmadi" (no existence leak). `stats` (orders, lastVisitAt, totalSpent) are computed over
  the scope's units.
* `list` (`GET /units/{id}/patients`): filters `unitId` (a sub-unit → its subtree), tag (exact); search: fold(fullName) contains OR (search digits ≥3 and phone contains digits) OR fold(passport) contains (**FIX** the mock's match-all bug); default sort createdAt desc.
* `search(query, limit=12)`: same predicate; empty query → all; rank by stats.lastVisitAt desc (nulls last).
* `findDuplicates(input)`: same country (`scope.countryId`); phone exact (normalized) OR passport case-insensitive OR pinfl exact; excludes nothing (client filters self).
* `create` (`POST /units/{id}/patients`): the registering facility = the path unit when it is one, else
  `registeredUnitId` (a facility under it) → 404 "Muassasa topilmadi" / 422 `facility_required`; `countryId` = the
  scope's country; checks in order → duplicate_passport (ci), duplicate_pinfl, then phone dup **only when neither passport nor pinfl given** → duplicate_phone. tags [], stats zero, portal.linked false. discountPercent default 0 (clamp 0..100).
* `update`: partial; phone normalised; **FIX** apply the same uniqueness rules (within the patient's country) excluding self.
* `countries()` / `regions(countryId?)` / `districts(regionId?)`: reference data (seeded by `db/v2/20_seed_reference.sql`
  / `python -m app.cli seed-reference`: countries, all 14 regions of Uzbekistan + districts); `regions` without
  `countryId` → the `DEFAULT_COUNTRY_CODE` country.

## 5. Catalog
* **Ownership**: every row (category, service type, schema, template, asset) carries `ownerUnitId` — the unit (any
  level) that defines it — and is **visible to the owner's whole subtree**. Management lists
  (`GET /units/{id}/categories | service-types | schemas`) show the unit's lineage: what it inherits from above plus
  everything owned in its subtree; creating under a unit makes that unit the owner. A row addressed by id may be
  read / changed when its owner is visible to the caller (inside their scope, or on the path above their units);
  superadmin any. Lists are cached per requesting unit (`catalog:{unitId}:categories|services`, 5 min); any
  catalogue write drops the whole `catalog:` namespace.
* `listCategories`: the lineage, sorted by order asc; all (active + inactive).
* `saveCategory`: create order = visible siblings (same parent) + 1; the parent must exist (404) and be visible AT
  the owner — owned by it or one of its ancestors — so a child is never shown where its parent is not (422);
  update: reject cycles (parentId ancestors) → 422 validation_error.
* `deleteCategory`: children → 409 has_children; services → 409 in_use; else soft delete.
* `listServiceTypes(q)`: categoryId → **transitive descendants**; activeOnly; `unitId` → only what is usable AT that unit (owner on the unit's path), each row resolved with `effectivePrice` and `available` (reception passes the cheque's facility); search name/code; sort order asc; each row gets `stats.ordered30d` = count of order items in the last 30 days (**FIX**: real 30-day window) over the owner's subtree ∩ the caller's scope.
* `saveServiceType`: create defaults price 0, unitPrices {}, disabledUnitIds [], turnaroundDays 1, order 99, isActive true, schemaId null, documentScope item, defaultTemplateId null; `code` unique per owner (409 "Bu kod band"); category and schema must be visible AT the owner (404); `unitPrices {unitId: price}` and `disabledUnitIds` are replaced wholesale and stored as rows of **`service_unit_settings (service_type_id, unit_id, is_enabled, price)`** — every id must be a live unit inside the owner's subtree, else 404 "Birlik topilmadi" (duplicates collapsed).
* **Per-unit price / availability (2026-10-29, v2)**: a `service_unit_settings` row applies to its unit **and the whole
  subtree**; **nearest level wins** — for on/off the nearest row on the facility's path, for the price the nearest
  row that carries a price; default = (the service is active AND its owner lies on the path, base price) — so a
  service the unit cannot see counts as unavailable. Effects: reception lists `unitId=<cheque facility>` and never
  shows a switched-off service there; `addItems` refuses it with 409 not_in_branch «…» xizmati bu **muassasada**
  ko‘rsatilmaydi; the services report's `unused` list skips services switched off in every facility of the scope.
  Prices of a switched-off unit are kept (switching back on restores them). Templates, schemas, existing order
  items, history and documents are untouched.
* `deleteServiceType`: any order items → 409 in_use; else soft delete.
* `listSchemas`: usedBy computed (count of service types using it). `getSchema` same.
* `saveSchema` update: `version += 1` iff status == published AND payload contains `fields`; status unchanged. Create: version 1, status draft, fields [].
* `publishSchema`: no fields → 422 empty; sets published; no version bump.

## 6. Templates
* `list(q)` (`GET /units/{id}/templates`): the lineage of the unit; status exact; serviceTypeId → `serviceTypeIds contains id OR serviceTypeIds empty`; `unitId` → templates **usable at** that unit: owner on the unit's path and either no `unitIds` or one of them on that path; search on name; sort updatedAt desc; full `doc` included.
* **Ownership and unit bindings**: `ownerUnitId` (any level) — usable in the owner's whole subtree; `unitIds` =
  rows of `template_units (template_id, unit_id)` that narrow it to some units **and their subtrees** (empty = the
  whole subtree); every id must lie inside the owner's subtree (404 "Birlik topilmadi"). **Resolution**
  (`list_active_templates(facility)`, cached `tpl:…` 120 s): active templates whose owner is on the facility's
  root → facility path and whose bindings are empty or include a unit on that path — the approval chain
  (explicit/default → active bound → active generic) and order-scope coverage only consider these.
* `save` update: `version += 1` iff payload has `doc` AND status == active; doc/serviceTypeIds/categoryIds/unitIds replaced wholesale. Create (`POST /units/{id}/templates`) defaults: owner = the path unit, name 'Yangi shablon', status draft, version 1, scope item, language uz, doc = emptyDoc (A4 portrait #ffffff margin 40, elements []), usage 0.
* `setStatus`: active with 0 elements → 422 empty. `duplicate {name?, unitIds?}`: name `"<name> (nusxa)"`, status draft, version 1, usage 0, bindings copied unless `unitIds` overrides them (a unit takes over another unit's template as its own copy this way). `delete`: active → 409 active; else soft delete.
* `default-receipt` (`POST /units/{id}/templates/default-receipt {name?, unitIds?, paper, language?}`): seeds the standard cheque (RENDERER_SPEC) as a draft owned by the path unit, bound to `unitIds` (empty = its whole subtree).
* `listAssets` / `uploadAsset({kind,name,url(dataURI),width,height,employeeId?})` (`/units/{id}/assets`, owned by the unit) → store bytes in stored_files; `url` returned = `/api/v1/files/{fileId}` (public GET, cacheable, sniffed mime).
* Render context: `org` (the organisation — the nearest non-facility unit above the facility: name, shortName,
  legalName, phone, email, address, logoUrl) and `unit` (the facility: name, shortName, code, kind, phone, email,
  address, timezone); `company` / `branch` stay as **aliases** of `org` / `unit` so documents authored before the
  tree keep rendering (RENDERER_SPEC §1).

## 7. Orders — core rules
**recompute(order)** after every mutation:
```
active = items(order) with status != cancelled
subtotal = Σ active.price (list price)            discountAmount = round(subtotal*discountPercent/100)
total = subtotal - discountAmount                 itemCount = len(active)
progress = counts by status over ALL items (keys pending, entered, submitted, approved, rejected, cancelled always present)
paidAmount = Σ payments(order) where refundedAt is null
payment = unpaid if paidAmount<=0 else paid if paidAmount>=total else partial
if status != cancelled: status = completed if (active and all approved) else open if payment==unpaid else in_progress
```
* **`unitId` = the facility** that did the work, on Order, OrderItem, Payment, ResultDocument (+ `unitName` on
  lists); the DB rejects any other level. Lists (`GET /units/{id}/orders | worklist | documents`, `…/orders/summary`,
  `…/orders/export.xlsx`) cover the URL unit's subtree, optionally narrowed by `unitId` (a facility, or a district /
  region with its facilities); a row addressed by id is loaded, then 403 when its facility is outside the
  employee's units.
* Order number: per-facility `code-000123` (`{facility.code}-{seq:06d}`), allocated atomically by
  `SELECT next_order_number(facility)` (`order_counters`, upsert `seq + 1`) at creation, never rolled back; unique
  per facility (`uq_orders_unit_number`).
* `create` (`POST /units/{id}/orders {patientId, unitId?, serviceTypeIds, note}`): the facility = the path unit when it is one, else `unitId` (a facility under it) → 404 "Muassasa topilmadi" / 422 `facility_required`; the patient must be visible in the caller's scope else 404 "Bemor topilmadi"; order: unitId = facility, status open, payment unpaid, discountPercent = patient.discountPercent (snapshot), patientName/phone snapshot; patient.stats.orders++, stats.lastVisitAt = max(existing, now); then addItems. Returns {order, items}.
* `addItems(orderId, ids)`: cancelled/completed → 409 closed; unknown ids skipped; a service not available at the cheque's facility (`effective_for_unit`: switched off on its path, inactive, or not visible there) → 409 not_in_branch; duplicates allowed; item: price = the effective price at the facility (nearest `service_unit_settings` price, else the base price); finalPrice = round(price − price*discount/100); status pending; schemaId/schemaVersion snapshot; values seeded: table fields → copy of presetRows, others null; serviceName/categoryName snapshots. Adding to a paid order flips payment to partial (allowed).
* `removeItem(reason?)` (permission reception.order.create — the registrar desk too): item must belong to order (**FIX**); status != pending → 409 in_progress (the lab has started); soft delete item (status stays; excluded from all queries); recompute. **Paid cheques too (2026-11-03):** whatever is then paid over the new total (`paidAmount − total`) goes back to the patient — the newest live payments are refunded whole (`refundReason` = `«<service>» chekdan o‘chirildi[: reason]`) until the excess is covered, and the part of the last one that stays paid is re-entered as a payment with the same method, cashier and original `createdAt` (note `Qayta hisoblandi — …`); patient totalSpent −excess; the response carries `refunded` (0 when only the remaining debt shrinks); audit `order.items.remove` records reason, refund and the refunded payment ids. (Before: a paid cheque answered 409 paid.)
* `pay(employeeId, {orderId, amount, method, sendSms})`: itemCount 0 → 422 empty; payment paid → 409 already_paid; amount ≤0 or > remaining → 422 amount; create payment (unitId = the cheque's facility); recompute; patient.stats.totalSpent += amount; if sendSms → outbox sms kind payment_receipt (text §10) queued. The first payment sets `orders.first_paid_at` (DB trigger).
* `refund(paymentId, reason)` (**new**, permission reception.payment.refund): sets refundedAt/reason; recompute; patient.totalSpent −= amount; payment status may become unpaid/partial; audit.
* `cancel(orderId, reason)`: payment != unpaid → 409 paid; status cancelled, cancelReason (also `note` kept as is — **FIX**: do not overwrite note; store cancel_reason separately but expose `note` unchanged), all items → cancelled, cancelledAt; recompute.
* `deleteOrder(orderId)` (**2026-10**, permission reception.order.delete): a cheque that never "happened" for accounting — every item must be pending|cancelled → else 409 in_progress "Laboratoriya boshlagan tahlilni o‘chirib bo‘lmaydi"; paid or not, the cheque is soft-deleted: order + items + payments get `deleted_at` (the rows stay in the DB — payment history and audit survive) and it drops out of every list, report and the dashboard; `patient.stats.orders − 1`, `stats.total_spent − paidAmount` (floor 0); audit `order.delete`. The order number stays consumed (the counter never rolls back).
* **Payment edit window (2026-11-04)**: reception changes a payment only within 24 h of taking it (`PAYMENT_EDIT_WINDOW`, measured from `payment.createdAt`); later → 403 payment_locked. Checked on: `refund` (that payment); `removeItem` (only the payments it gives money back from — dropping a service that just shrinks the debt stays open, and money back comes from the newest payments first); `deleteOrder` (every alive payment, refunded ones too). Paying, adding services and cancelling an unpaid cheque are not limited. `reception.payment.late_edit` lifts the window and on its own also opens these three corrections (no desk permissions needed) — the system `admin` and `rahbar` roles have it (admins can grant it to any role); superadmin always can. The cheque list carries `firstPaidAt` (oldest payment, refunded or not) so the UI can lock the delete button.
* `changePaymentMethod(paymentId, {parts, reason?})` (**2026-11-04**, `PUT /payments/{id}/method`; permission reception.payment.create or late_edit): corrects how a taken payment was paid — the amount, `createdAt`, cashier (`employeeId`) and facility (`unitId`) stay, so the money keeps its day and cashier in every report. Parts (1–4, same method summed) must add up to the payment's amount → else 422 parts; one part with the current method → 422 same_method; refunded → 409 state "Qaytarilgan to‘lovni o‘zgartirib bo‘lmaydi"; payment window as above (403 payment_locked after 24 h for reception). One part: the row gets the new method. Several: the row keeps the part of its current method (else the first) and the rest become new rows with the same createdAt / employeeId / unitId / note. Every touched row appends to `corrections` `{at, byId, byName, fromMethod, fromAmount, reason, splitFrom?}` (shown on the cheque); no SMS, patient stats unchanged; audit `payment.method_change` (before, parts, reason, newPaymentIds).
* **Category binding**: employees with non-empty `categoryIds` (except superadmin) are restricted to those categories + descendants: `worklist` is filtered server-side (explicit `categoryIds` are intersected), and saveValues/submit/reject/approveItem/approveOrder on an item outside them → 403 forbidden "Bu kategoriya sizga biriktirilmagan".
* `worklist(q)` (`GET /units/{id}/worklist`, `…/worklist/counts`): items with schemaId not null; `unitId` narrowing; categoryIds (exact membership); status[]; dateFrom/dateTo on item.createdAt (half-open day range in Asia/Tashkent — **FIX**); order.payment != unpaid (paid or partial); join orderNumber, patientName, patientPhone (order snapshots), patientGender, patientBirthDate (live patient), unitName; search on patientName/orderNumber/serviceName; default sort createdAt desc.
* `saveValues(itemId, employeeId, values, labNote?)`: approved → 409 approved; cancelled → 409 state "Bekor qilingan tahlil" (**FIX**); values replaced wholesale; labNote assigned as given (null clears); technicianId/name; enteredAt = now; pending|rejected → entered (clear rejectReason **FIX**); entered stays; submitted stays submitted.
* `submitItem`: status not in (entered, submitted) → 409 state "Avval natijalarni saqlang"; required check against current schema fields: missing if value null/''/empty list → 422 required "To‘ldirilmagan: labels"; **toggle**: submitted → entered (submittedAt null) / entered → submitted (submittedAt now).
* `approveItem(itemId, employeeId, templateId?)`: status != submitted → 409 state; template chain over the templates active at the cheque's facility (§6): (1) templateId ?? serviceType.defaultTemplateId (any status), (2) first active template with serviceTypeIds ∋ st or categoryIds ∋ cat, (3) first active template with empty serviceTypeIds, else 422 no_template "Bu xizmat uchun faol shablon yo‘q". Item → approved, doctorId/name, approvedAt; document {orderItemId, unitId, title "<serviceName> — natija", status final, templateVersion snapshot, deliveries [portal delivered, sms queued], snapshot of render context}; PDF rendered & stored; template.usage++; recompute; SMS result_ready (§10) + Telegram PDF push if patient linked.
* `orderScopeItems(orderId, templateId)`: template 404; returns non-cancelled items covered by template (serviceTypeIds ∋ item.st OR categoryIds ∋ item.cat OR both lists empty) in any status.
* `approveOrder(orderId, employeeId, templateId, itemIds?)`: template missing/not active at the facility → 422 no_template "Faol shablon topilmadi"; scope != order → 422 scope; covered = coveredItems ∩ status ∈ (submitted, approved); if itemIds → filter; toApprove = submitted ⊆ covered; empty → 409 state "Tasdiqlash uchun yuborilgan tahlil yo‘q"; ONE document {orderItemId = toApprove[0], orderItemIds = all covered ids, title = template.name}; approve toApprove (same timestamp); covered items without documentId get it; usage++ once; recompute; SMS "<tpl.name>: N ta tahlil natijasi tayyor. …". Returns {items: covered, document}.
* `rejectItem(itemId, employeeId, reason)`: status != submitted → 409 state; rejected, rejectReason, submittedAt null; recompute.
* `listDocuments({orderId?, patientId?, unitId?})` (`GET /units/{id}/documents`) scoped to the URL unit's subtree (**FIX**); patientId → documents of that patient's orders (all statuses); sort createdAt desc. `getDocument(id)`: 404 "Hujjat topilmadi"; 403 when its facility is outside the employee's units.
* `GET documents/{id}/pdf` → stored PDF (render on demand if missing). `GET /orders/{id}/receipt.pdf` → the cheque rendered with the receipt template active at its facility (422 no_template when none).

* **Result trail (2026-10):** `order_items.history` = `[{type, at, byId, byName, reason?, documentId?}]`, oldest first:
  `submitted` / `unsubmitted` (lab toggles), `returned` (doctor sends a submitted result back, reason), `approved`
  (documentId), `revoked` (doctor takes an approved result back, reason, documentId), `reopened` (an approved item went
  back to approval because the order-scope document it shared was revoked). Items from before have their trail rebuilt
  from `submittedAt` / `approvedAt` / `rejectReason` until their next event. The portal never gets the trail.
* **Public result link (2026-10, SMS / Telegram):** `GET /d/{token}` returns public info (title, organisation, issue
  date — no private data) and may be opened any number of times. The PDF itself is always behind an SMS code:
  `POST /d/{token}/otp/request` sends the code to the phone the document belongs to (the cheque's phone, else the
  patient's; shown masked in the response) through the SMS account in effect for the **cheque's facility** (the one
  that sent the result SMS — its own account, else the nearest inherited one; an `off` account on the way = none,
  `sms_account_for`). When nobody can send, the request answers 409 `sms_not_configured` instead of "code sent" (dev
  mode still returns `devCode`); no valid phone on the cheque or the patient → 409 `no_phone`; a running resend
  cooldown → 429 with the real remaining `retryAfter`. The OTP outbox row carries the cheque (`order_id`) and the
  sending facility (`unit_id`).
  `POST /d/{token}/otp/verify` returns the PDF and counts the view (`viewed_at`, `public_view_count`). Revoked
  documents answer 410 `revoked` on every endpoint.
* **Code-free window (2026-10-30):** `settings.documentFreeViewDays` (0–90, default 0 = a code is always needed — the
  rule above), read as the **effective setting of the cheque's facility** (inherited from the district / region /
  country node, nearest wins, §3). For that many days after a document is issued (`created_at`) its SMS link opens
  the PDF without a code: `GET /d/{token}` answers `otpRequired: false` + `freeUntil`, and `POST /d/{token}/open`
  returns the PDF (no-store; counts the view like the OTP path; audit `document_public_open`; rate-limited per IP and
  per token). Outside the window `open` answers 403 `otp_required` and the page falls back to the SMS code. The
  window is computed from the current setting on every request — lowering it (or 0) closes the links immediately.
  Set on the SMS settings page (`PUT /units/{id}` `documentFreeViewDays`, allowed with admin.settings.write — on
  the facility or any unit above it). The QR page (`/v/`) is not affected: it never opens the PDF.
* **QR authenticity check (2026-10-29):** every issued document gets a second token, `verify_token`, of its own; the
  QR element renders `{doc.link}` = `<web app>/v/{verify_token}`, frozen into the render snapshot at approval
  (RENDERER_SPEC §3.9). `GET /v/{token}` answers only whether the paper is genuine — `status` `valid` | `revoked`
  (the result was revoked, or its cheque cancelled / deleted; `revokedAt`), title, organisation (`orgName`),
  facility (`unitName`), cheque number, approving doctor, issue time and the patient masked to initials
  (`H*** D***`). Never the values, the PDF, the phone or an SMS code: the verify token is not accepted by `/d/…`, and
  the SMS token not by `/v/…`. Unknown token → 404 (the paper is not from this system). Scans are unlimited and not
  counted. `ResultDocumentOut.verifyLink` is the exact QR value of the document (documents issued before 2026-10-29
  have no `verify_token`; their QR — and `verifyLink` — keep the SMS link).
* **Revoke (`POST /items/{id}/revoke {reason}`, confirm.result.approve, reason required):** approved → `rejected` with
  `rejectReason`; approvedAt / doctor / documentId / submittedAt cleared; the document gets `status = revoked` and is
  soft-deleted (PDF link, portal, reports drop it; the public link answers 410 `revoked`); approved items sharing that
  order-scope document → `submitted` (`reopened`). The lab corrects and submits again; approval issues a new document
  and a new SMS.

## 8. Messaging / notifications
* **Accounts** (`messaging_accounts (unit_id, channel sms|telegram, provider xabarchi|none, api_key_enc, priority,
  is_active)`): attached to any unit and inherited downwards — `resolve_messaging_account(unit, channel)` /
  `sms_account_for(unit)` pick the **nearest active** account on the facility's path; an `sms` account with
  provider `none` on the way switches SMS off for that subtree (→ no account). Managed per unit in §3.
* `listOutbox` (`GET /units/{id}/outbox`, `…/outbox/counts`; messaging.send | reports.operations.read): messages sent under the unit (every facility of the subtree); `unitId[]` narrowing; status/kind exact; search: to contains digits (only when digits present) OR fold(text); sort createdAt desc fixed. Every row carries `unitId` (the sending facility).
* `send({to[], text, kind, scheduledAt?, unitId?})` (`POST /units/{id}/messages/send`): the sending facility = the path unit when it is one, else `unitId` (422 `facility_required`); one message per recipient (normalise phones, dedupe); status scheduled if scheduledAt future else queued; channel sms; permission messaging.send, >1 recipient requires messaging.broadcast. Real dispatch by the outbox worker → Xabarchi `POST /api/v1/public/messages` (X-API-Key) with the key of the account the message was queued with while it can still send, otherwise whatever account is in effect for its facility now; no usable account → `failed` with error code `sms_not_configured`; provider id stored; retries with backoff (max attempts) → failed with error text. Services queue optional SMS through `enqueue_sms_if_configured(unit)` — nothing is recorded when no account is in effect (`sms` = `not_configured` in their answers).
* `notifications()`: for everyone, for my units (or the units above them) or addressed to me (`employee_id` null or = me), newest first, limit 50; `read` = me ∈ read_by. `markRead(id?)`: mark one or all visible.
* System notifications created by services: e.g. approve failure of SMS, submitted items pending approval (daily aggregate optional).

## 9. Reports
* Every report (`GET /units/{id}/reports/…`) is over the URL unit's subtree, optionally narrowed by `unitId` to a
  sub-unit's **subtree** (a district covers its facilities; a facility is just itself; a unit outside the scope
  matches nothing). Cached per (unit, narrowing, range) 30 s.
* Date range: `dateFrom`/`dateTo` (YYYY-MM-DD) inclusive, evaluated in Asia/Tashkent (**FIX** vs UTC).
* `dashboard`: todayOrders (today, non-cancelled), todayRevenue (Σ payments today, non-refunded), pendingLab (items pending|entered, all time), pendingApproval (submitted, all time), patients (patients of the scope — membership rule §4), smsQueued (queued|scheduled under the scope), trend: dense daily series over range with orders count + revenue (Σ paidAmount of orders created that day — keep mock semantics), byCategory: items in range excluding cancelled (**FIX** consistent with breakdown), rolled up to top-level category `{name, count, revenue(Σ finalPrice), color}` sorted revenue desc.
* `breakdown(by=category|service|unit|employee)`: items in range, non-cancelled, grouped by name (category name / service name / **facility name** / technicianName or '—'), `{name, count, revenue}` sorted revenue desc. (`by=branch` is gone — `unit`.)
* `GET /units/{id}/reports/export.xlsx` — the breakdowns as an Excel workbook (reports.export).

* Patient reports (2026-10), all over the period's non-cancelled cheques, unit-scoped like the rest, cached 30 s;
  money fields are null without `reports.finance.read`:
  * `patients` — distinct patients; "new" = their first non-cancelled cheque (in the same units) is in the period;
    gender, age groups (0-17/18-29/30-44/45-59/60+), districts, daily patients/new, top 10 by cheques, debts.
  * `results` — items in the lab / at approval / approved / overdue (cheque time + service `turnaround_days`), average
    hours cheque→approval, on time, per category; documents received vs not. **Received** = opened by the patient
    (`/d/{token}` or the portal: `viewed_at`, `view_count`), printed by staff (`POST /documents/{id}/printed`:
    `printed_at`, `print_count`), or Telegram sent the PDF. Opens/prints are recorded from 2026-10-07 (`trackingSince`).
  * `results/list?status=not_received|received|waiting` — paged rows (documents, or cheques still waiting), search.
  * `services` — per service: items, patients, Σ final price, not ready, avg hours; the same-length previous period's
    count; active catalog services nobody ordered in the period (`unused` — services switched off in every facility
    of the scope are skipped).

## 10. SMS texts (rendered for the ORDER's facility, **FIX**)
* Placeholders: **`{org}`** = the organisation the facility belongs to — the nearest non-facility ancestor's
  `shortName or name` (`org_name`; "Xorazm SES"); **`{unit}`** = the facility's name; `{company}` / `{branch}` are kept
  as **aliases** of `{org}` / `{unit}` so texts written before the tree keep working; plus `{patient} {order}
  {service} {count} {link} {code} {login} {password}` and, on the receipt, `{amount}` (this payment), `{total}` (the
  cheque total), `{remaining}` (still to pay after it) — money as `120 000` (ru-RU grouping, U+00A0). An empty
  placeholder never leaves a double space behind.
* Platform defaults (`messaging/service.py DEFAULT_TEMPLATES`):
  * payment_receipt: `Chek {order}: {amount} so‘m qabul qilindi. Natijalar tayyor bo‘lganda xabar beramiz. {org}`
  * result_ready (item): `{service} natijasi tayyor: {link} {org}`
  * result_ready (order): `{service}: {count} ta tahlil natijasi tayyor: {link} {org}` (`{service}` = the template name) — a customised `result_ready` text also wins for order-scope approvals
  * reminder: `Hurmatli {patient}! Sizni {org} klinikasida kutamiz. Chek: {order}`
  * otp: `Sizning tasdiqlash kodingiz: {code}` (uz) — the Telegram bot uses its own per-language text.
  * portal_access (2026-11-04): `{org} shaxsiy kabinetiga kirish: {link} Login: {login} Parol: {password}`, `{link}` =
    `<web app>/login`; stored masked (`Parol: ••••••`), sent from the encrypted payload; not kept without an SMS account.
* `{link}` (2026-10) = `<web app>/d/<document.public_token>` — the web app the approving staff used (Origin, only if it
  is an allowed CORS origin), else FRONTEND_URL; the app page `/d/:token` opens the PDF from `GET /api/v1/d/{token}`.
  Available in unit overrides of `result_ready` too.
* **Per-unit overrides** (`sms_templates (unit_id, key, lang, body)`, `GET/PUT /units/{id}/sms-templates`, §3) for
  `payment_receipt`, `result_ready`, `reminder` — when set, they win. Resolution **per key**: the facility's own
  text → the nearest ancestor's → the platform default (an empty value = the default). `applyToAll` writes the
  same texts to every unit under the saving unit (they stop inheriting). Messages always render with the order's
  facility (`{unit}` = that facility, `{org}` = its organisation).

## 11. Portal (patient token)
* Identity: an SMS-code session acts for every alive patient sharing its phone (the whole registry); a portal-login
  session (§2, `via=login`) for its own patient only — ownership checks of orders / documents use that set.
* `overview`: patient projection (no note/discount/tags/pinfl; `registeredUnitId`), orders (every facility, non-cancelled, newest first), documents of those orders (newest first), **`orgs`** [PortalOrgOut — the organisation behind each cheque: the nearest non-facility unit above its facility (letterhead data: name, legalName, phone, email, address, logo)] and **`units`** [PortalUnitOut — the facilities the cheques were written at: name, code, kind, address, phone, `orgId`], both in order of first appearance.
* `order(id)`: order must belong to patient else 404 (**FIX**: no existence leak); items (all statuses) but `values`/`labNote` only for approved items (**FIX**), documents, `org`, `unit`.
* `document(id)`: {document, template, item?, order, items (covered), schemas, serviceCodes{stId: code|id}, category?, org, unit}; PDF at `/portal/documents/{id}/pdf`.

## 12. Telegram bot (per messaging account; NavbatApp parity)
* **One bot per `messaging_accounts` row** with channel `telegram`, attached to a unit and inherited downwards;
  the bot manager is keyed by account id. `api_key_enc` NULL on such a row = the **platform bot** whose token comes
  from the environment (`TELEGRAM_BOT_TOKEN`) — production runs one platform bot on the country node for everyone;
  a unit may connect its own bot (`PUT /units/{id}/telegram`, §3). Patients' links (`telegram_links`) and chat
  preferences are kept per account (the bot), not per organisation.
* /start → language picker (uz, uzc, ru, en) → phone → patient lookup in the **bot's country registry** (the account unit's country) → OTP SMS (through the patient's home unit) → link chat (patient.telegram_chat_id, telegram_links) → main keyboard: 🧾 Mening cheklarim (last 10 orders: number, date, services, sum, paid status), 💰 To‘lovlarim (last 10 paid + total), 📄 Natijalarim (send PDF documents, newest 20), 🚪 Chiqish (unlink), 🌐 Til.
* Push: payment receipt (if linked) and result PDF on approval (document delivery channel telegram). An outbox row
  is delivered by the account it was queued with while it is active, else by the nearest Telegram account of the
  facility that sent it (`resolve_messaging_account(unit, 'telegram')`).
* Webhook (optional, polling is the default): `POST /telegram/webhook/{accountId}/{secret}` — accepted only when
  `telegramWebhookSecret` in the account unit's effective settings matches.
