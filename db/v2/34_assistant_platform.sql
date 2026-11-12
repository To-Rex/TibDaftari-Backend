-- ============================================================================
-- TemoDB v2 · 34 · platform-level assistant account
--   assistant_accounts.unit_id NULL = the account of the WHOLE platform (every country, region, facility): entered
--   once by a superadmin, used wherever no unit on the path has its own account or switched the assistant off.
--   Units keep overriding it (own) or switching it off for their subtree exactly as before.
-- Idempotent: re-running changes nothing.
-- ============================================================================

ALTER TABLE assistant_accounts ALTER COLUMN unit_id DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_assistant_accounts_platform_alive ON assistant_accounts ((1)) WHERE unit_id IS NULL AND deleted_at IS NULL;
COMMENT ON COLUMN assistant_accounts.unit_id IS 'NULL = platforma darajasi (barcha davlat va muassasalar); yo‘ldagi birlikning o‘z hisobi yoki «o‘chirilgan» holati ustun keladi';
