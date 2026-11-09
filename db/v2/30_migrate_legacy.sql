-- ============================================================================
-- TemoDB v2 · 30 · move the production data into the new structure
--
-- Source: schema `legacy` (postgres_fdw view of the production copy).
-- Scope : the two real organisations —
--           Xorazm viloyati SES (company 01a00cdd-6ec5-75b7-977d-18df3d0eab10) → region node "Xorazm viloyati"
--           Nur Klinika         (company 01a00cdd-6ec6-799d-99e4-6c9c426c09c8) → facility "Nur Klinika"
--         214 soft-deleted test companies + "T-verify reset…" (test fixtures) are NOT migrated.
-- Rules :
--   company_id  → owner/scope unit (region node for the SES, the facility for Nur Klinika)
--   branch_id   → unit_id (facility ids are identical to the old branch ids)
--   employees   : superadmin → no unit; admin role or no branches → company node (company-wide, as the
--                 backend's branch_scope() works today); otherwise first branch = home, others = employee_units
--   patients    → one registry per country (all UZ); registered_unit = old branch, else the first cheque's unit
--   catalogue   : branch_prices / disabled_branch_ids → service_unit_settings; template branch_ids → template_units
--   messaging   : company & branch SMS columns → messaging_accounts; settings.smsTemplates → sms_templates;
--                 the platform Telegram bot (token in env) → one country-level telegram account
--   sessions / OTP codes are transient and are not copied (everyone signs in again)
-- Everything runs in one transaction; a report is written to legacy.migration_report.
-- ============================================================================

CREATE TABLE IF NOT EXISTS legacy.migration_report (
  step  text        NOT NULL,
  rows  bigint      NOT NULL,
  note  text,
  at    timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION legacy.note(step text, n bigint, note text DEFAULT NULL) RETURNS void
LANGUAGE sql AS $$ INSERT INTO legacy.migration_report (step, rows, note) VALUES (step, n, note) $$;

-- ---------------------------------------------------------------------------
-- maps
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE m_company (company_id uuid PRIMARY KEY, unit_id uuid NOT NULL, country_id uuid NOT NULL, label text) ON COMMIT DROP;
INSERT INTO m_company VALUES
  ('01a00cdd-6ec5-75b7-977d-18df3d0eab10', '01a1be00-0000-7000-8000-000000000002', '01a0b069-7749-7d10-87b0-703892974a40', 'Xorazm SES → Xorazm viloyati'),
  ('01a00cdd-6ec6-799d-99e4-6c9c426c09c8', '01a00cdd-6ec6-79a0-863e-b562c9444189', '01a0b069-7749-7d10-87b0-703892974a40', 'Nur Klinika → Nur Klinika (muassasa)');

CREATE TEMP TABLE m_branch (branch_id uuid PRIMARY KEY, unit_id uuid NOT NULL) ON COMMIT DROP;
INSERT INTO m_branch
SELECT b.id, u.id FROM legacy.branches b JOIN m_company c ON c.company_id = b.company_id JOIN org_units u ON u.id = b.id AND u.level = 'facility';

DO $$
DECLARE missing text;
BEGIN
  SELECT string_agg(b.name || ' (' || b.code || ')', ', ') INTO missing
  FROM legacy.branches b JOIN m_company c ON c.company_id = b.company_id
  WHERE b.deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM m_branch m WHERE m.branch_id = b.id);
  IF missing IS NOT NULL THEN RAISE EXCEPTION 'live branches without a facility unit: %', missing; END IF;
END $$;
SELECT legacy.note('branches mapped to facilities', (SELECT count(*) FROM m_branch));

-- ---------------------------------------------------------------------------
-- 1. organisation nodes take over the company / branch profile
-- ---------------------------------------------------------------------------
UPDATE org_units u SET
  legal_name = COALESCE(NULLIF(c.legal_name, ''), u.legal_name),
  phone      = COALESCE(u.phone, NULLIF(c.phone, '')),
  email      = COALESCE(u.email, NULLIF(c.email, '')),
  address    = COALESCE(u.address, NULLIF(c.address, '')),
  is_active  = c.is_active,
  settings   = u.settings || (c.settings - 'seed' - 'smsTemplates')
               || CASE WHEN c.locale <> 'uz' THEN jsonb_build_object('locale', c.locale) ELSE '{}'::jsonb END,
  created_at = LEAST(u.created_at, c.created_at)
FROM legacy.companies c JOIN m_company m ON m.company_id = c.id
WHERE u.id = m.unit_id;

UPDATE org_units u SET
  is_active  = b.is_active,
  settings   = u.settings || (b.settings - 'smsTemplates'),
  created_at = LEAST(u.created_at, b.created_at)
FROM legacy.branches b JOIN m_branch m ON m.branch_id = b.id
WHERE u.id = m.unit_id;
SELECT legacy.note('org_units profiled from companies/branches', (SELECT count(*) FROM m_company) + (SELECT count(*) FROM m_branch));

-- ---------------------------------------------------------------------------
-- 2. files
-- ---------------------------------------------------------------------------
INSERT INTO stored_files (id, unit_id, sha256, mime, size, filename, storage, data, object_key, is_public, created_at, created_by)
SELECT f.id, m.unit_id, f.sha256, f.mime, f.size, f.filename, 'db', f.data, NULL, f.is_public, f.created_at AT TIME ZONE 'UTC', f.created_by
FROM legacy.stored_files f LEFT JOIN m_company m ON m.company_id = f.company_id
WHERE f.company_id IS NULL OR m.company_id IS NOT NULL;
SELECT legacy.note('stored_files', (SELECT count(*) FROM stored_files),
  'skipped (test companies): ' || (SELECT count(*) FROM legacy.stored_files f WHERE f.company_id IS NOT NULL AND f.company_id NOT IN (SELECT company_id FROM m_company)));

-- ---------------------------------------------------------------------------
-- 3. staff
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE m_emp ON COMMIT DROP AS
SELECT e.*, r.key AS role_key,
       CASE
         WHEN e.is_super_admin THEN NULL
         WHEN r.key = 'admin' OR cardinality(e.branch_ids) = 0 THEN mc.unit_id
         ELSE COALESCE((SELECT mb.unit_id FROM m_branch mb WHERE mb.branch_id = e.branch_ids[1]), mc.unit_id)
       END AS new_unit_id
FROM legacy.employees e
JOIN legacy.roles r ON r.id = e.role_id
JOIN m_company mc ON mc.company_id = e.company_id;

DO $$
DECLARE unknown text;
BEGIN
  SELECT string_agg(DISTINCT role_key, ', ') INTO unknown
  FROM m_emp WHERE role_key NOT IN (SELECT key FROM roles WHERE scope_unit_id IS NULL AND deleted_at IS NULL);
  IF unknown IS NOT NULL THEN RAISE EXCEPTION 'employees use roles that have no system role: %', unknown; END IF;
END $$;

INSERT INTO employees (id, unit_id, role_id, full_name, login, password_hash, phone, email, overrides, category_ids, status,
                       avatar_hue, is_super_admin, last_login_at, failed_logins, locked_until, signature_asset_id,
                       created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT e.id, e.new_unit_id, pr.id, e.full_name, e.login, e.password_hash, e.phone, e.email, e.overrides, e.category_ids, e.status,
       e.avatar_hue, e.is_super_admin, e.last_login_at, e.failed_logins, e.locked_until, NULL,
       e.created_by, e.created_at, e.updated_at, e.deleted_at, e.deleted_by
FROM m_emp e JOIN roles pr ON pr.scope_unit_id IS NULL AND pr.deleted_at IS NULL AND pr.key = e.role_key;
SELECT legacy.note('employees', (SELECT count(*) FROM employees),
  format('superadmin=%s, company-wide (admin / no branch)=%s, single facility=%s, multi-facility=%s',
         (SELECT count(*) FROM m_emp WHERE is_super_admin),
         (SELECT count(*) FROM m_emp WHERE NOT is_super_admin AND (role_key = 'admin' OR cardinality(branch_ids) = 0)),
         (SELECT count(*) FROM m_emp WHERE NOT is_super_admin AND role_key <> 'admin' AND cardinality(branch_ids) = 1),
         (SELECT count(*) FROM m_emp WHERE NOT is_super_admin AND role_key <> 'admin' AND cardinality(branch_ids) > 1)));

INSERT INTO employee_units (employee_id, unit_id)
SELECT DISTINCT e.id, mb.unit_id
FROM m_emp e CROSS JOIN LATERAL unnest(e.branch_ids) AS b(bid)
JOIN m_branch mb ON mb.branch_id = b.bid
WHERE NOT e.is_super_admin AND e.role_key <> 'admin' AND cardinality(e.branch_ids) > 1 AND mb.unit_id <> e.new_unit_id
ON CONFLICT DO NOTHING;
SELECT legacy.note('employee_units (extra facilities)', (SELECT count(*) FROM employee_units));

-- ---------------------------------------------------------------------------
-- 4. catalogue
-- ---------------------------------------------------------------------------
INSERT INTO categories (id, owner_unit_id, parent_id, name, code, icon, color, sort_order, is_active, phone, workflow,
                        created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT c.id, m.unit_id, c.parent_id, c.name, c.code, c.icon, c.color, c."order", c.is_active, c.phone, c.workflow,
       c.created_by, c.created_at, c.updated_at, c.deleted_at, c.deleted_by
FROM legacy.categories c JOIN m_company m ON m.company_id = c.company_id;
SELECT legacy.note('categories', (SELECT count(*) FROM categories));

INSERT INTO attribute_schemas (id, owner_unit_id, name, description, version, status, fields, created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT s.id, m.unit_id, s.name, s.description, s.version, s.status, s.fields, s.created_by, s.created_at, s.updated_at, s.deleted_at, s.deleted_by
FROM legacy.attribute_schemas s JOIN m_company m ON m.company_id = s.company_id;
SELECT legacy.note('attribute_schemas', (SELECT count(*) FROM attribute_schemas));

INSERT INTO result_templates (id, owner_unit_id, name, description, status, version, service_type_ids, category_ids, scope, language, doc,
                              thumbnail_url, usage, created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT t.id, m.unit_id, t.name, t.description, t.status, t.version, t.service_type_ids, t.category_ids, t.scope, t.language, t.doc,
       t.thumbnail_url, t.usage, t.created_by, t.created_at, t.updated_at, t.deleted_at, t.deleted_by
FROM legacy.result_templates t JOIN m_company m ON m.company_id = t.company_id;
SELECT legacy.note('result_templates', (SELECT count(*) FROM result_templates));

INSERT INTO template_units (template_id, unit_id)
SELECT DISTINCT t.id, mb.unit_id
FROM legacy.result_templates t JOIN m_company m ON m.company_id = t.company_id
CROSS JOIN LATERAL unnest(t.branch_ids) AS b(bid) JOIN m_branch mb ON mb.branch_id = b.bid;
SELECT legacy.note('template_units (from branch_ids)', (SELECT count(*) FROM template_units));

INSERT INTO service_types (id, owner_unit_id, category_id, name, code, description, price, turnaround_days, sort_order, is_active,
                           schema_id, document_scope, default_template_id, created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT s.id, m.unit_id, s.category_id, s.name, s.code, s.description, s.price, s.turnaround_days, s."order", s.is_active,
       s.schema_id, s.document_scope, s.default_template_id, s.created_by, s.created_at, s.updated_at, s.deleted_at, s.deleted_by
FROM legacy.service_types s JOIN m_company m ON m.company_id = s.company_id;
SELECT legacy.note('service_types', (SELECT count(*) FROM service_types));

WITH k AS (
  SELECT s.id AS service_type_id, kv.key::uuid AS branch_id, NULLIF(kv.value, '')::bigint AS price
  FROM legacy.service_types s JOIN m_company m ON m.company_id = s.company_id
  CROSS JOIN LATERAL jsonb_each_text(s.branch_prices) kv
  WHERE kv.key ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  UNION ALL
  SELECT s.id, d, NULL
  FROM legacy.service_types s JOIN m_company m ON m.company_id = s.company_id
  CROSS JOIN LATERAL unnest(s.disabled_branch_ids) AS d
)
INSERT INTO service_unit_settings (service_type_id, unit_id, is_enabled, price, updated_at)
SELECT k.service_type_id, mb.unit_id,
       NOT EXISTS (SELECT 1 FROM legacy.service_types s2 WHERE s2.id = k.service_type_id AND k.branch_id = ANY (s2.disabled_branch_ids)),
       max(k.price),
       now()
FROM k JOIN m_branch mb ON mb.branch_id = k.branch_id
GROUP BY k.service_type_id, mb.unit_id, k.branch_id;
SELECT legacy.note('service_unit_settings (branch prices / disabled)', (SELECT count(*) FROM service_unit_settings),
  'price overrides=' || (SELECT count(*) FROM service_unit_settings WHERE price IS NOT NULL) || ', disabled=' || (SELECT count(*) FROM service_unit_settings WHERE NOT is_enabled));

INSERT INTO template_assets (id, owner_unit_id, kind, name, file_id, width, height, employee_id, created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT a.id, m.unit_id, a.kind, a.name, a.file_id,
       CASE WHEN a.width  > 0 THEN a.width  ELSE 1 END,
       CASE WHEN a.height > 0 THEN a.height ELSE 1 END,
       a.employee_id, a.created_by, a.created_at, a.updated_at, a.deleted_at, a.deleted_by
FROM legacy.template_assets a JOIN m_company m ON m.company_id = a.company_id;
SELECT legacy.note('template_assets', (SELECT count(*) FROM template_assets));

UPDATE employees e SET signature_asset_id = l.signature_asset_id
FROM legacy.employees l
WHERE l.id = e.id AND l.signature_asset_id IS NOT NULL AND EXISTS (SELECT 1 FROM template_assets a WHERE a.id = l.signature_asset_id);

-- ---------------------------------------------------------------------------
-- 5. patients — national registry
-- ---------------------------------------------------------------------------
INSERT INTO patients (id, country_id, registered_unit_id, full_name, phone, phone_extra, gender, birth_date, passport_number, pinfl,
                      region_id, district_id, street, workplace, discount_percent, contract_number, note, tags,
                      stats_orders, stats_last_visit_at, stats_total_spent, portal_linked, telegram_chat_id, portal_last_login_at,
                      portal_login, portal_password_hash, portal_password_set_at, portal_failed_logins, portal_locked_until,
                      created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT p.id, m.country_id, mb.unit_id, p.full_name, p.phone, p.phone_extra, p.gender, p.birth_date, p.passport_number, p.pinfl,
       p.region_id, p.district_id, p.street, p.workplace, p.discount_percent, p.contract_number, p.note, p.tags::text[],
       p.stats_orders, p.stats_last_visit_at, p.stats_total_spent, p.portal_linked, p.telegram_chat_id, p.portal_last_login_at,
       p.portal_login, p.portal_password_hash, p.portal_password_set_at, p.portal_failed_logins, p.portal_locked_until,
       p.created_by, p.created_at, p.updated_at, p.deleted_at, p.deleted_by
FROM legacy.patients p JOIN m_company m ON m.company_id = p.company_id LEFT JOIN m_branch mb ON mb.branch_id = p.branch_id;
SELECT legacy.note('patients', (SELECT count(*) FROM patients));

-- ---------------------------------------------------------------------------
-- 6. cheques, services on them, payments, documents
-- ---------------------------------------------------------------------------
INSERT INTO orders (id, unit_id, number, patient_id, patient_name, patient_phone, created_by_employee_id, status, payment,
                    subtotal, discount_percent, discount_amount, total, paid_amount, item_count, progress, note, first_paid_at,
                    cancelled_at, cancel_reason, completed_at, created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT o.id, mb.unit_id, o.number, o.patient_id, o.patient_name, o.patient_phone, o.created_by_employee_id, o.status, o.payment,
       o.subtotal, o.discount_percent, o.discount_amount, o.total, o.paid_amount, o.item_count, o.progress, o.note, NULL,
       o.cancelled_at, o.cancel_reason, o.completed_at, o.created_by, o.created_at, o.updated_at, o.deleted_at, o.deleted_by
FROM legacy.orders o JOIN m_company m ON m.company_id = o.company_id JOIN m_branch mb ON mb.branch_id = o.branch_id;
SELECT legacy.note('orders', (SELECT count(*) FROM orders),
  'skipped (test companies): ' || (SELECT count(*) FROM legacy.orders o WHERE o.company_id NOT IN (SELECT company_id FROM m_company)));

INSERT INTO order_items (id, order_id, unit_id, service_type_id, service_name, category_id, category_name, price, final_price, status,
                         schema_id, schema_version, "values", technician_id, technician_name, entered_at, submitted_at, doctor_id, doctor_name,
                         approved_at, reject_reason, document_id, lab_note, history, created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT i.id, i.order_id, mb.unit_id, i.service_type_id, i.service_name, i.category_id, i.category_name, i.price, i.final_price, i.status,
       i.schema_id, i.schema_version, i."values", i.technician_id, i.technician_name, i.entered_at, i.submitted_at, i.doctor_id, i.doctor_name,
       i.approved_at, i.reject_reason, NULL, i.lab_note, i.history, i.created_by, i.created_at, i.updated_at, i.deleted_at, i.deleted_by
FROM legacy.order_items i JOIN m_company m ON m.company_id = i.company_id JOIN m_branch mb ON mb.branch_id = i.branch_id;
SELECT legacy.note('order_items', (SELECT count(*) FROM order_items));

INSERT INTO payments (id, order_id, unit_id, amount, method, employee_id, note, refunded_at, refund_reason, corrections,
                      created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT p.id, p.order_id, mb.unit_id, p.amount, p.method, p.employee_id, p.note, p.refunded_at, p.refund_reason, p.corrections,
       p.created_by, p.created_at, p.updated_at, p.deleted_at, p.deleted_by
FROM legacy.payments p JOIN m_company m ON m.company_id = p.company_id JOIN m_branch mb ON mb.branch_id = p.branch_id
ORDER BY p.created_at;
SELECT legacy.note('payments', (SELECT count(*) FROM payments));

-- exact first_paid_at: the earliest live payment (the insert trigger only approximates in bulk)
UPDATE orders o SET first_paid_at = fp.at
FROM (SELECT order_id, min(created_at) AS at FROM payments WHERE deleted_at IS NULL GROUP BY order_id) fp
WHERE fp.order_id = o.id;
UPDATE orders o SET first_paid_at = NULL
WHERE NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id AND p.deleted_at IS NULL);

INSERT INTO result_documents (id, order_id, patient_id, unit_id, order_item_id, order_item_ids, template_id, template_version, title, status,
                              pdf_file_id, deliveries, snapshot, public_token, verify_token, viewed_at, last_viewed_at, view_count,
                              printed_at, print_count, public_view_count, public_first_viewed_at, public_last_viewed_at,
                              created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT d.id, d.order_id, d.patient_id, mb.unit_id, d.order_item_id, d.order_item_ids, d.template_id, d.template_version, d.title, d.status,
       d.pdf_file_id, d.deliveries, d.snapshot, d.public_token, d.verify_token, d.viewed_at, d.last_viewed_at, d.view_count,
       d.printed_at, d.print_count, d.public_view_count, d.public_first_viewed_at, d.public_last_viewed_at,
       d.created_by, d.created_at, d.updated_at, d.deleted_at, d.deleted_by
FROM legacy.result_documents d
JOIN m_company m ON m.company_id = d.company_id
JOIN legacy.orders o ON o.id = d.order_id
JOIN m_branch mb ON mb.branch_id = o.branch_id;
SELECT legacy.note('result_documents', (SELECT count(*) FROM result_documents));

UPDATE order_items i SET document_id = l.document_id
FROM legacy.order_items l
WHERE l.id = i.id AND l.document_id IS NOT NULL AND EXISTS (SELECT 1 FROM result_documents d WHERE d.id = l.document_id);

-- ---------------------------------------------------------------------------
-- 7. messaging: accounts, texts, outbox, notifications, telegram
-- ---------------------------------------------------------------------------
-- company-level SMS settings → the company node
INSERT INTO messaging_accounts (unit_id, channel, provider, api_key_enc, api_key_masked, default_priority, sender_note, is_active, created_at)
SELECT m.unit_id, 'sms', CASE WHEN c.sms_provider = 'xabarchi' AND c.sms_api_key_enc IS NOT NULL THEN 'xabarchi' ELSE 'none' END,
       CASE WHEN c.sms_provider = 'xabarchi' THEN c.sms_api_key_enc END, CASE WHEN c.sms_provider = 'xabarchi' THEN c.sms_api_key_masked END,
       COALESCE(c.sms_default_priority, 'transactional'), NULLIF(c.sms_sender_note, ''), true, c.created_at
FROM legacy.companies c JOIN m_company m ON m.company_id = c.id;
-- branch-level SMS settings (own key or explicitly switched off) → the facility
INSERT INTO messaging_accounts (unit_id, channel, provider, api_key_enc, api_key_masked, default_priority, sender_note, is_active, created_at)
SELECT mb.unit_id, 'sms', CASE WHEN b.sms_provider = 'xabarchi' AND b.sms_api_key_enc IS NOT NULL THEN 'xabarchi' ELSE 'none' END,
       CASE WHEN b.sms_provider = 'xabarchi' THEN b.sms_api_key_enc END, CASE WHEN b.sms_provider = 'xabarchi' THEN b.sms_api_key_masked END,
       COALESCE(b.sms_default_priority, 'transactional'), NULLIF(b.sms_sender_note, ''), true, b.created_at
FROM legacy.branches b JOIN m_branch mb ON mb.branch_id = b.id
WHERE b.sms_provider IS NOT NULL
ON CONFLICT DO NOTHING;
-- company Telegram bot (if a company had one) → company node; otherwise the platform bot from env → country node
INSERT INTO messaging_accounts (unit_id, channel, provider, api_key_enc, api_key_masked, bot_username, sender_note, is_active, created_at)
SELECT m.unit_id, 'telegram', 'telegram', c.telegram_bot_token_enc, c.telegram_bot_token_masked, c.telegram_bot_username, NULL, true, c.created_at
FROM legacy.companies c JOIN m_company m ON m.company_id = c.id
WHERE c.telegram_bot_token_enc IS NOT NULL;
INSERT INTO messaging_accounts (id, unit_id, channel, provider, api_key_enc, bot_username, sender_note, is_active)
VALUES ('01a1be00-0000-7000-8000-0000000000b0', '01a1be00-0000-7000-8000-000000000001', 'telegram', 'telegram', NULL, NULL,
        'Platforma boti — token TELEGRAM_* muhit o‘zgaruvchilaridan', true);
SELECT legacy.note('messaging_accounts', (SELECT count(*) FROM messaging_accounts),
  (SELECT string_agg(u.code || ':' || a.channel || '/' || a.provider, ' ' ORDER BY u.code) FROM messaging_accounts a JOIN org_units u ON u.id = a.unit_id));

-- SMS texts: company → company node, branch → facility
INSERT INTO sms_templates (unit_id, key, lang, body, is_active, created_at)
SELECT m.unit_id, kv.key, 'uz', kv.value, true, c.created_at
FROM legacy.companies c JOIN m_company m ON m.company_id = c.id
CROSS JOIN LATERAL jsonb_each_text(COALESCE(c.settings -> 'smsTemplates', '{}'::jsonb)) kv
WHERE kv.key ~ '^[a-z][a-z0-9_]{1,39}$' AND NULLIF(btrim(kv.value), '') IS NOT NULL
UNION ALL
SELECT mb.unit_id, kv.key, 'uz', kv.value, true, b.created_at
FROM legacy.branches b JOIN m_branch mb ON mb.branch_id = b.id
CROSS JOIN LATERAL jsonb_each_text(COALESCE(b.settings -> 'smsTemplates', '{}'::jsonb)) kv
WHERE kv.key ~ '^[a-z][a-z0-9_]{1,39}$' AND NULLIF(btrim(kv.value), '') IS NOT NULL;
SELECT legacy.note('sms_templates', (SELECT count(*) FROM sms_templates));

INSERT INTO outbox_messages (id, unit_id, account_id, patient_id, order_id, document_id, channel, kind, "to", text, status, scheduled_at, sent_at,
                             attempts, next_attempt_at, leased_until, provider_message_id, error, payload,
                             created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT o.id, mb.unit_id, NULL, o.patient_id, o.order_id, o.document_id, o.channel, o.kind, o."to", o.text, o.status, o.scheduled_at, o.sent_at,
       o.attempts, o.next_attempt_at, o.leased_until, o.provider_message_id, o.error, o.payload,
       o.created_by, o.created_at, o.updated_at, o.deleted_at, o.deleted_by
FROM legacy.outbox_messages o JOIN m_company m ON m.company_id = o.company_id LEFT JOIN m_branch mb ON mb.branch_id = o.branch_id;
-- which account would have sent it (historical attribution)
UPDATE outbox_messages o SET account_id = (resolve_messaging_account(o.unit_id, 'sms')).id WHERE o.channel = 'sms' AND o.unit_id IS NOT NULL;
UPDATE outbox_messages o SET account_id = '01a1be00-0000-7000-8000-0000000000b0' WHERE o.channel = 'telegram';
SELECT legacy.note('outbox_messages', (SELECT count(*) FROM outbox_messages));

INSERT INTO notifications (id, unit_id, employee_id, title, body, kind, link, read_by, created_by, created_at, updated_at, deleted_at, deleted_by)
SELECT n.id, m.unit_id, n.employee_id, n.title, n.body, n.kind, n.link, n.read_by, n.created_by, n.created_at, n.updated_at, n.deleted_at, n.deleted_by
FROM legacy.notifications n JOIN m_company m ON m.company_id = n.company_id;
SELECT legacy.note('notifications', (SELECT count(*) FROM notifications));

INSERT INTO telegram_links (id, account_id, patient_id, chat_id, lang, linked_at, unlinked_at)
SELECT l.id, '01a1be00-0000-7000-8000-0000000000b0', l.patient_id, l.chat_id, l.lang, l.linked_at, l.unlinked_at
FROM legacy.telegram_links l JOIN m_company m ON m.company_id = l.company_id
WHERE EXISTS (SELECT 1 FROM patients p WHERE p.id = l.patient_id)
ON CONFLICT DO NOTHING;
INSERT INTO telegram_chat_prefs (account_id, chat_id, lang, updated_at)
SELECT DISTINCT ON (p.chat_id) '01a1be00-0000-7000-8000-0000000000b0', p.chat_id, p.lang, p.updated_at
FROM legacy.telegram_chat_prefs p JOIN m_company m ON m.company_id = p.company_id
ORDER BY p.chat_id, p.updated_at DESC;
SELECT legacy.note('telegram_links / chat_prefs', (SELECT count(*) FROM telegram_links) + (SELECT count(*) FROM telegram_chat_prefs));

-- ---------------------------------------------------------------------------
-- 8. audit log (real organisations + system rows)
-- ---------------------------------------------------------------------------
INSERT INTO audit_log (id, created_at, country_id, unit_id, actor_type, actor_id, action, entity, entity_id, before, after, ip, request_id)
SELECT a.id, a.created_at, m.country_id, m.unit_id, a.actor_type, a.actor_id, a.action, a.entity, a.entity_id, a.before, a.after, a.ip, a.request_id
FROM legacy.audit_log a LEFT JOIN m_company m ON m.company_id = a.company_id
WHERE a.company_id IS NULL OR m.company_id IS NOT NULL;
SELECT setval('audit_log_id_seq', GREATEST((SELECT max(id) FROM audit_log), 1));
SELECT legacy.note('audit_log', (SELECT count(*) FROM audit_log));

-- ---------------------------------------------------------------------------
-- 9. finishing touches
-- ---------------------------------------------------------------------------
-- patients registered before branches existed: the unit of their first cheque
UPDATE patients p SET registered_unit_id = fo.unit_id
FROM (SELECT DISTINCT ON (patient_id) patient_id, unit_id FROM orders WHERE deleted_at IS NULL ORDER BY patient_id, created_at) fo
WHERE p.id = fo.patient_id AND p.registered_unit_id IS NULL;
SELECT legacy.note('patients without a registering unit (no cheque yet)', (SELECT count(*) FROM patients WHERE registered_unit_id IS NULL));

-- cheque counters never fall behind the numbers already issued
UPDATE order_counters c SET seq = GREATEST(c.seq, x.mx), updated_at = now()
FROM (SELECT unit_id, max(split_part(number, '-', 2)::integer) AS mx FROM orders WHERE number ~ '^[A-Za-z0-9]+-[0-9]+$' GROUP BY unit_id) x
WHERE x.unit_id = c.unit_id;

SELECT legacy.note('sessions / otp_challenges', 0, 'not migrated on purpose (transient) — everyone signs in again');

ANALYZE;

-- ---------------------------------------------------------------------------
-- report
-- ---------------------------------------------------------------------------
SELECT step, rows, note FROM legacy.migration_report ORDER BY at, step;
