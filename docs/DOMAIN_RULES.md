# Domain rules — behavioural contract (derived from the frontend mock repositories)

The frontend (Clinic-Web) currently runs on `src/data/mock/repos/*.mock.ts`. The backend reproduces
that behaviour 1:1 (wire shape + rules) **except** where marked **FIX** (deliberate corrections) —
those are bugs in the mock that must not be reproduced. When in doubt, read the mock source.

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
  Backend implementation: store nothing extra; apply the same folding in SQL via an IMMUTABLE `fold_text(text)` PL/pgSQL function + pg_trgm GIN index on `fold_text(col)` for patients/orders; or filter in Python for small sets (catalog).
* Create paths ignore any client-supplied `id`. Timestamps `createdAt/updatedAt` are server-owned.
* Money rounding: `round()` half-up.

## 1. Error catalogue (status / code / message)
| status | code | message |
|---|---|---|
| 401 | auth_error | Hisob topilmadi · Login yoki parol noto‘g‘ri · Sessiya tugagan |
| 401 | otp_expired | Kod muddati tugagan |
| 401 | otp_invalid | Kod noto‘g‘ri |
| 403 | inactive | Hisob faol emas |
| 403 | forbidden | Ruxsat yo‘q |
| 404 | not_found | Kompaniya topilmadi · Filial topilmadi · Xodim topilmadi · Rol topilmadi · Bemor topilmadi · Bu raqam bilan bemor topilmadi. Klinikaga murojaat qiling. · Kategoriya topilmadi · Xizmat topilmadi · Sxema topilmadi · Shablon topilmadi · Chek topilmadi · Tahlil topilmadi · Bemor yoki filial topilmadi · Hujjat topilmadi |
| 409 | conflict | Bu login band |
| 409 | in_use | Bu rol xodimlarga biriktirilgan · Kategoriyada xizmatlar bor · Bu xizmat bo‘yicha buyurtmalar bor — o‘chirish o‘rniga nofaol qiling |
| 409 | has_children | Avval ichki kategoriyalarni o‘chiring |
| 409 | active | Faol shablonni o‘chirib bo‘lmaydi — avval arxivlang |
| 409 | closed | Chek yopilgan |
| 409 | in_progress | Laboratoriya boshlagan tahlilni o‘chirib bo‘lmaydi |
| 409 | paid | To‘langan chekdan xizmat o‘chirib bo‘lmaydi · To‘langan chekni bekor qilish uchun avval qaytarish qiling |
| 409 | already_paid | Chek allaqachon to‘langan |
| 409 | approved | Tasdiqlangan natijani o‘zgartirib bo‘lmaydi |
| 409 | state | Avval natijalarni saqlang · Faqat yuborilgan natijani tasdiqlash mumkin · Tasdiqlash uchun yuborilgan tahlil yo‘q · Faqat yuborilgan natijani qaytarish mumkin |
| 409 | duplicate_passport / duplicate_pinfl / duplicate_phone | Bu passport raqami bilan bemor mavjud · Bu JSHSHIR bilan bemor mavjud · Bu telefon raqami bilan bemor mavjud |
| 422 | invalid_phone | Telefon raqam noto‘g‘ri |
| 422 | empty | Kamida bitta maydon kerak · Bo‘sh shablonni faollashtirib bo‘lmaydi · Chekda xizmat yo‘q |
| 422 | amount | Summa 1 – {remaining} oralig‘ida bo‘lishi kerak  (raw integer, en dash) |
| 422 | required | To‘ldirilmagan: {labels joined ', '} |
| 422 | no_template | Bu xizmat uchun faol shablon yo‘q · Faol shablon topilmadi |
| 422 | scope | Bu shablon chek darajasidagi hujjat emas |
| 429 | rate_limited | Juda ko‘p urinish. Keyinroq urinib ko‘ring. |

## 2. Auth
* Permission catalogue (32 keys `<module>.<resource>.<action>`): reception.patient.read/write, reception.order.create/cancel, reception.payment.create/refund, lab.worklist.read, lab.result.write/submit, confirm.result.read/approve/resend, reports.finance.read, reports.operations.read, reports.export, messaging.send/broadcast, admin.company.read/write, admin.branch.write, admin.employee.read/write, admin.role.write, admin.catalog.read/write, admin.schema.write, admin.template.read/write/publish, admin.settings.write, platform.company.manage.
  Effective = role.permissions ∪ overrides.allow − overrides.deny (deny wins).
* System roles: `superadmin` (companyId null, all 32), `admin` (per company, all except platform.*), plus registrator/laborant/vrach/rahbar (seed).
* **StaffSession** `{actor:'staff', employeeId, companyId, branchId, isSuperAdmin, roleKey, fullName, permissions[], accessToken, expiresAt}`; `branchId = branchIds[0] if len==1 else null`; `isSuperAdmin = employee.is_super_admin` (**FIX**: not derived from a client-editable role key); `roleKey = role.key`; staff token TTL 12h (sliding: `staffMe` refreshes expiresAt).
* `staffLogin`: login trimmed + case-insensitive; password argon2 verify; wrong either → 401 auth_error "Login yoki parol noto‘g‘ri"; inactive → 403 inactive; on success set lastLoginAt; **FIX** rate limit + lockout (settings) → 429.
* `staffMe(token)`: rebuild session from DB each call (role changes apply immediately); revoked/expired → 401 "Sessiya tugagan".
* Patient OTP: `requestPatientOtp(phone)`: digits; 9 digits → prefix 998; must match `^998\d{9}$` else 422 invalid_phone; find patient by phone across all companies (first by created_at) else 404 "Bu raqam bilan bemor topilmadi. Klinikaga murojaat qiling."; challenge TTL 5 min; code random N digits (settings.otp_length; **dev mode: returned as devCode**), delivered as SMS via the patient's company outbox (kind otp) when the company has an SMS provider; resend cooldown 60s per phone (429).
  `verifyPatientOtp(challengeId, code, phone)`: unknown/expired → 401 otp_expired; wrong → 401 otp_invalid (max 3 attempts then expired); success → consume, patient.portal.linked = true, session TTL 30 days.
* `logout(token)`: revoke jti (Redis + sessions.revoked_at). Idempotent.

## 3. Tenant / staff
* `listCompanies` (superadmin only): search on name/legalName; branchCount/employeeCount computed; default sort createdAt desc.
* `saveCompany`: partial merge; `sms` object replaced wholesale (frontend sends the whole object). Accept `sms.apiKey` (plaintext, write-only) → encrypt, store masked `apiKeyMasked` = `xab_live_••••••••XXXX`; never return plaintext. `slug` unique. Create defaults: locale uz, isActive true, sms {provider none, defaultPriority transactional}.
* `saveBranch`: **FIX** 404 when id unknown; `code` unique per company (409 conflict "Bu filial kodi band"); `orderSeq` server-owned. Defaults: timezone Asia/Tashkent, isActive true, orderSeq 0.
* `listEmployees`: filters branchId (array contains), roleId, status, search on fullName/login/phone(digits); default sort fullName asc.
* `saveEmployee`: `password` handled separately (argon2); create: login unique (case-insensitive, global) → 409 conflict "Bu login band"; default password `123456` if none; avatarHue random 0..359 persisted; update: replace branchIds/categoryIds/overrides wholesale; login change allowed if still unique. Never return password data.
* `setOverrides`: replace `{allow, deny}` wholesale (validate keys ⊂ catalogue).
* `listRoles(companyId)`: company roles + platform roles (companyId null).
* `saveRole`: **FIX** 404 on unknown id; key unique per company; reserved key `superadmin` (only platform); system roles: name/description/permissions editable, key/isSystem not. Create defaults: key = slug of name or generated, permissions [], isSystem false.
* `deleteRole`: in use → 409 in_use; system role → 409 in_use "Tizim rolini o‘chirib bo‘lmaydi"; else soft delete.

## 4. Patients
* `normPhone(p)`: digits; 9 → prefix 998. Stored `998XXXXXXXXX`.
* `list`: filters companyId, tag (exact); search: fold(fullName) contains OR (search digits ≥3 and phone contains digits) OR fold(passport) contains (**FIX** the mock's match-all bug); default sort createdAt desc.
* `search(query, limit=12)`: same predicate; empty query → all; rank by stats.lastVisitAt desc (nulls last).
* `findDuplicates(input)`: same company; phone exact (normalized) OR passport case-insensitive OR pinfl exact; excludes nothing (client filters self).
* `create`: checks in order → duplicate_passport (ci), duplicate_pinfl, then phone dup **only when neither passport nor pinfl given** → duplicate_phone. tags [], stats zero, portal.linked false. discountPercent default 0 (clamp 0..100).
* `update`: partial; phone normalised; **FIX** apply the same uniqueness rules excluding self.
* `regions()` / `districts(regionId?)`: reference data (seeded: all 14 regions of Uzbekistan + districts).

## 5. Catalog
* `listCategories`: sorted by order asc; all (active + inactive).
* `saveCategory`: create order = siblings(company, parent)+1; update: reject cycles (parentId ancestors) → 422 validation_error.
* `deleteCategory`: children → 409 has_children; services → 409 in_use; else soft delete.
* `listServiceTypes(q)`: categoryId → **transitive descendants**; activeOnly; search name/code; sort order asc; each row gets `stats.ordered30d` = count of order items in the last 30 days (**FIX**: real 30-day window).
* `saveServiceType`: create defaults price 0, branchPrices {}, turnaroundDays 1, order 99, isActive true, schemaId null, documentScope item, defaultTemplateId null; branchPrices replaced wholesale.
* `deleteServiceType`: any order items → 409 in_use; else soft delete.
* `listSchemas`: usedBy computed (count of service types using it). `getSchema` same.
* `saveSchema` update: `version += 1` iff status == published AND payload contains `fields`; status unchanged. Create: version 1, status draft, fields [].
* `publishSchema`: no fields → 422 empty; sets published; no version bump.

## 6. Templates
* `list(q)`: status exact; serviceTypeId → `serviceTypeIds contains id OR serviceTypeIds empty`; search on name; sort updatedAt desc; full `doc` included.
* `save` update: `version += 1` iff payload has `doc` AND status == active; doc/serviceTypeIds/categoryIds replaced wholesale. Create defaults: name 'Yangi shablon', status draft, version 1, scope item, language uz, doc = emptyDoc (A4 portrait #ffffff margin 40, elements []), usage 0.
* `setStatus`: active with 0 elements → 422 empty. `duplicate`: name `"<name> (nusxa)"`, status draft, version 1, usage 0, bindings copied. `delete`: active → 409 active; else soft delete.
* `listAssets` / `uploadAsset({kind,name,url(dataURI),width,height,employeeId?})` → store bytes in stored_files; `url` returned = `/api/v1/files/{fileId}` (public GET, cacheable, sniffed mime).

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
* Order number: per-branch `code-000123` (`{branch.code}-{seq:06d}`), allocated atomically (`UPDATE branches SET order_seq=order_seq+1 … RETURNING`) at creation, never rolled back.
* `create(companyId, employeeId, {patientId, branchId, serviceTypeIds, note})`: patient & branch must exist in the company else 404 "Bemor yoki filial topilmadi"; order: status open, payment unpaid, discountPercent = patient.discountPercent (snapshot), patientName/phone snapshot; patient.stats.orders++, stats.lastVisitAt = max(existing, now); then addItems. Returns {order, items}.
* `addItems(orderId, ids)`: cancelled/completed → 409 closed; unknown ids skipped; duplicates allowed; item: price = branchPrices[branchId] ?? price; finalPrice = round(price − price*discount/100); status pending; schemaId/schemaVersion snapshot; values seeded: table fields → copy of presetRows, others null; serviceName/categoryName snapshots. Adding to a paid order flips payment to partial (allowed).
* `removeItem`: item must belong to order (**FIX**); status != pending → 409 in_progress; order.payment != unpaid → 409 paid; soft delete item (status stays; excluded from all queries).
* `pay(employeeId, {orderId, amount, method, sendSms})`: itemCount 0 → 422 empty; payment paid → 409 already_paid; amount ≤0 or > remaining → 422 amount; create payment; recompute; patient.stats.totalSpent += amount; if sendSms → outbox sms kind payment_receipt (text §10) queued.
* `refund(paymentId, reason)` (**new**, permission reception.payment.refund): sets refundedAt/reason; recompute; patient.totalSpent −= amount; payment status may become unpaid/partial; audit.
* `cancel(orderId, reason)`: payment != unpaid → 409 paid; status cancelled, cancelReason (also `note` kept as is — **FIX**: do not overwrite note; store cancel_reason separately but expose `note` unchanged), all items → cancelled, cancelledAt; recompute.
* `worklist(q)`: items with schemaId not null; branchId; categoryIds (exact membership); status[]; dateFrom/dateTo on item.createdAt (half-open day range in Asia/Tashkent — **FIX**); order.payment != unpaid (paid or partial); join orderNumber, patientName, patientPhone (order snapshots), patientGender, patientBirthDate (live patient); search on patientName/orderNumber/serviceName; default sort createdAt desc.
* `saveValues(itemId, employeeId, values, labNote?)`: approved → 409 approved; cancelled → 409 state "Bekor qilingan tahlil" (**FIX**); values replaced wholesale; labNote assigned as given (null clears); technicianId/name; enteredAt = now; pending|rejected → entered (clear rejectReason **FIX**); entered stays; submitted stays submitted.
* `submitItem`: status not in (entered, submitted) → 409 state "Avval natijalarni saqlang"; required check against current schema fields: missing if value null/''/empty list → 422 required "To‘ldirilmagan: labels"; **toggle**: submitted → entered (submittedAt null) / entered → submitted (submittedAt now).
* `approveItem(itemId, employeeId, templateId?)`: status != submitted → 409 state; template chain: (1) templateId ?? serviceType.defaultTemplateId (any status), (2) first active template with serviceTypeIds ∋ st or categoryIds ∋ cat, (3) first active template with empty serviceTypeIds, else 422 no_template "Bu xizmat uchun faol shablon yo‘q". Item → approved, doctorId/name, approvedAt; document {orderItemId, title "<serviceName> — natija", status final, templateVersion snapshot, deliveries [portal delivered, sms queued], snapshot of render context}; PDF rendered & stored; template.usage++; recompute; SMS result_ready (§10) + Telegram PDF push if patient linked.
* `orderScopeItems(orderId, templateId)`: template 404; returns non-cancelled items covered by template (serviceTypeIds ∋ item.st OR categoryIds ∋ item.cat OR both lists empty) in any status.
* `approveOrder(orderId, employeeId, templateId, itemIds?)`: template missing/not active → 422 no_template "Faol shablon topilmadi"; scope != order → 422 scope; covered = coveredItems ∩ status ∈ (submitted, approved); if itemIds → filter; toApprove = submitted ⊆ covered; empty → 409 state "Tasdiqlash uchun yuborilgan tahlil yo‘q"; ONE document {orderItemId = toApprove[0], orderItemIds = all covered ids, title = template.name}; approve toApprove (same timestamp); covered items without documentId get it; usage++ once; recompute; SMS "<tpl.name>: N ta tahlil natijasi tayyor. …". Returns {items: covered, document}.
* `rejectItem(itemId, employeeId, reason)`: status != submitted → 409 state; rejected, rejectReason, submittedAt null; recompute.
* `listDocuments({orderId?, patientId?})` scoped to caller's company (**FIX**); patientId → documents of that patient's orders (all statuses); sort createdAt desc. `getDocument(id)`: company-scoped 404.
* `GET documents/{id}/pdf` → stored PDF (render on demand if missing).

## 8. Messaging / notifications
* `listOutbox`: status/kind exact; search: to contains digits (only when digits present) OR fold(text); sort createdAt desc fixed.
* `send({to[], text, kind, scheduledAt?})`: one message per recipient (normalise phones, dedupe); status scheduled if scheduledAt future else queued; channel sms; permission messaging.send, >1 recipient requires messaging.broadcast. Real dispatch by the outbox worker → Xabarchi `POST /api/v1/public/messages` (X-API-Key) with the company key; provider id stored; retries with backoff (max attempts) → failed with error text.
* `notifications()`: current employee's company, employee_id null or = me, newest first, limit 50; `read` = me ∈ read_by. `markRead(id?)`: mark one or all.
* System notifications created by services: e.g. approve failure of SMS, submitted items pending approval (daily aggregate optional).

## 9. Reports
* Date range: `dateFrom`/`dateTo` (YYYY-MM-DD) inclusive, evaluated in the company timezone (Asia/Tashkent) (**FIX** vs UTC).
* `dashboard`: todayOrders (today, non-cancelled), todayRevenue (Σ payments today, non-refunded), pendingLab (items pending|entered, all time), pendingApproval (submitted, all time), patients (company total), smsQueued (queued|scheduled), trend: dense daily series over range with orders count + revenue (Σ paidAmount of orders created that day — keep mock semantics), byCategory: items in range excluding cancelled (**FIX** consistent with breakdown), rolled up to top-level category `{name, count, revenue(Σ finalPrice), color}` sorted revenue desc.
* `breakdown(by=category|service|branch|employee)`: items in range, non-cancelled, grouped by name (category name / service name / branch name / technicianName or '—'), `{name, count, revenue}` sorted revenue desc.

## 10. SMS texts (company name = the ORDER's company, **FIX**)
* payment_receipt: `Chek {order.number}: {amount:ru-RU grouping, U+00A0} so‘m qabul qilindi. Natijalar tayyor bo‘lganda xabar beramiz. {company.name}`
* result_ready (item): `{serviceName} natijasi tayyor. Portalda ko‘rishingiz mumkin. {company.name}`
* result_ready (order): `{template.name}: {n} ta tahlil natijasi tayyor. Portalda ko‘rishingiz mumkin. {company.name}`
* otp: `Sizning tasdiqlash kodingiz: {code}` (uz) — Telegram bot uses its own per-language text.
* Optional per-company overrides in `companies.settings.smsTemplates` `{payment_receipt, result_ready, reminder}` with placeholders `{patient} {order} {service} {company}` — when set, they win.

## 11. Portal (patient token)
* `overview`: patient projection (no note/discount/tags/pinfl), orders (all companies, non-cancelled, newest first), documents of those orders (newest first), companies [{id,name}].
* `order(id)`: order must belong to patient else 404 (**FIX**: no existence leak); items (all statuses) but `values`/`labNote` only for approved items (**FIX**), documents.
* `document(id)`: {document, template, item?, order, items (covered), schemas, serviceCodes{stId: code|id}, category?}; PDF at `/portal/documents/{id}/pdf`.

## 12. Telegram bot (per company; NavbatApp parity)
* /start → language picker (uz, uzc, ru, en) → phone → patient lookup in company → OTP SMS → link chat (patient.telegram_chat_id, telegram_links) → main keyboard: 🧾 Mening cheklarim (last 10 orders: number, date, services, sum, paid status), 💰 To‘lovlarim (last 10 paid + total), 📄 Natijalarim (send PDF documents, newest 20), 🚪 Chiqish (unlink), 🌐 Til.
* Push: payment receipt (if linked) and result PDF on approval (document delivery channel telegram).
