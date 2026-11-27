-- ============================================================================
-- TemoDB v2 · 42 · the training seen from above («o‘quv nazorati»)
--   employee_training.unavailable   the tasks that could not be practised where the employee works when they last
--                                   looked (no service with a result form there …): they do not hold the training
--                                   back, and a manager must see the same picture the employee sees
--   employee_training.reminded_at   when the managers were last told this person has not finished (a reminder is
--                                   sent after TRAINING_STALL_DAYS and repeated every TRAINING_REMIND_EVERY_DAYS)
--   notifications.code / params     what a notice the system writes is ABOUT, so the interface can say it in the
--                                   reader's language; title/body stay as the Uzbek text for everything else
-- Idempotent: re-running changes nothing.
-- ============================================================================

ALTER TABLE employee_training ADD COLUMN IF NOT EXISTS unavailable jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE employee_training ADD COLUMN IF NOT EXISTS reminded_at timestamptz;
COMMENT ON COLUMN employee_training.unavailable IS 'Xodim ishlayotgan joyda mashq qilib bo''lmaydigan topshiriqlar kalitlari (hisobga olinmaydi)';
COMMENT ON COLUMN employee_training.reminded_at IS 'Rahbarlarga "o''quvni tugatmagan" eslatmasi oxirgi marta qachon yuborilgan';

-- the reminder looks for people who started long ago and were not mentioned lately
CREATE INDEX IF NOT EXISTS ix_employee_training_started ON employee_training (started_at) WHERE status = 'active';

ALTER TABLE notifications ADD COLUMN IF NOT EXISTS code varchar(40);
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS params jsonb;
COMMENT ON COLUMN notifications.code IS 'Tizim yozgan xabarnoma turi (masalan training.passed) — interfeys o''quvchi tilida ko''rsatadi';
COMMENT ON COLUMN notifications.params IS 'code uchun qiymatlar (ism, bo''lim, sonlar)';
