-- ============================================================================
-- TemoDB v2 · 10 · messaging accounts: allow a Telegram bot whose token comes
-- from the deployment environment (TELEGRAM_* env), not from the database.
-- Production runs one platform bot for everyone; patients are linked to it.
-- api_key_enc NULL on a telegram account = "use the environment token".
-- ============================================================================

ALTER TABLE messaging_accounts DROP CONSTRAINT ck_messaging_accounts_key;
ALTER TABLE messaging_accounts ADD CONSTRAINT ck_messaging_accounts_key
  CHECK (provider <> 'xabarchi' OR api_key_enc IS NOT NULL);
COMMENT ON COLUMN messaging_accounts.api_key_enc IS
  'Shifrlangan Xabarchi kaliti yoki bot tokeni. Telegram uchun NULL = token muhit (env) dan olinadi — platforma boti.';
