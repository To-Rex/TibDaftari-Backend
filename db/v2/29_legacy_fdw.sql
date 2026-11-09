-- ============================================================================
-- TemoDB v2 · 29 · read the production copy through postgres_fdw
-- The production database was restored (pg_dump → pg_restore) into a sibling
-- database `legacy_tibdaftari` on this same Postgres instance. This script
-- exposes it as schema `legacy` so 30_migrate_legacy.sql can SELECT from it.
-- Connection goes over the local Unix socket (trust for the postgres superuser
-- in the official image) — no password is stored here.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS postgres_fdw;

CREATE SERVER legacy_srv FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (dbname 'legacy_tibdaftari', fetch_size '2000');
CREATE USER MAPPING FOR postgres SERVER legacy_srv OPTIONS (password_required 'false');

CREATE SCHEMA legacy;
IMPORT FOREIGN SCHEMA public
  LIMIT TO (companies, branches, roles, employees, patients, categories, service_types, attribute_schemas,
            result_templates, template_assets, stored_files, orders, order_items, payments, result_documents,
            outbox_messages, notifications, telegram_links, telegram_chat_prefs, audit_log, sessions, otp_challenges)
  FROM SERVER legacy_srv INTO legacy;
COMMENT ON SCHEMA legacy IS 'Production (TibDaftari) nusxasiga postgres_fdw orqali faqat-o''qish ko''rinishi — ko''chirish manbai';
