-- ============================================================================
-- TemoDB v2 · 37 · a second SMS provider: Eskiz.uz next to Xabarchi
--   messaging_accounts.provider gains 'eskiz' (channel sms). An Eskiz account keeps the cabinet e-mail in
--   `login`, the password Fernet-encrypted in `api_key_enc` (like a Xabarchi key) and the sender name (nick,
--   e.g. 4546) in `sender`. Attached to a country node it serves every facility of the country; any unit below may
--   still keep its own Xabarchi / Eskiz account or switch SMS off — the nearest account on the path wins, as before.
-- Idempotent: re-running changes nothing.
-- ============================================================================

ALTER TABLE messaging_accounts DROP CONSTRAINT IF EXISTS ck_messaging_accounts_provider;
ALTER TABLE messaging_accounts ADD CONSTRAINT ck_messaging_accounts_provider CHECK (
     (channel = 'sms'      AND provider IN ('xabarchi', 'eskiz', 'none'))
  OR (channel = 'telegram' AND provider = 'telegram'));
ALTER TABLE messaging_accounts ADD COLUMN IF NOT EXISTS login  varchar(200);
ALTER TABLE messaging_accounts ADD COLUMN IF NOT EXISTS sender varchar(40);
COMMENT ON COLUMN messaging_accounts.provider IS 'sms: xabarchi | eskiz | none (none = bu birlik va ostidagilar SMS yubormaydi); telegram: telegram';
COMMENT ON COLUMN messaging_accounts.login    IS 'Eskiz: kabinet e-mail (login); Xabarchi va Telegram uchun bo''sh';
COMMENT ON COLUMN messaging_accounts.sender   IS 'Eskiz: yuboruvchi nomi (nick), masalan 4546';
