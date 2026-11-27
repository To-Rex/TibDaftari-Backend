-- ============================================================================
-- TemoDB v2 · 43 · the training again («o‘quvni takrorlash»)
--   An employee may go through the training again — the whole plan as on the first day, or only the tasks they
--   want to brush up — started by themselves or by a manager.
--   employee_training.chosen      the task keys of a partial plan; NULL = every task of the role
--   employee_training.previous    where a repeat the employee started THEMSELVES returns to when they leave it
--                                 (the row as it was before: status, tasks, dates). NULL = the training is
--                                 mandatory: only finishing — or a manager — ends it
--   employee_training.attempt     which time this is (1 = the first training)
--   employee_training.started_by  who put the employee (back) into the training: themselves or a manager;
--                                 NULL = it began at their first sign-in
-- Idempotent: re-running changes nothing.
-- ============================================================================

ALTER TABLE employee_training ADD COLUMN IF NOT EXISTS chosen jsonb;
ALTER TABLE employee_training ADD COLUMN IF NOT EXISTS previous jsonb;
ALTER TABLE employee_training ADD COLUMN IF NOT EXISTS attempt integer NOT NULL DEFAULT 1;
ALTER TABLE employee_training ADD COLUMN IF NOT EXISTS started_by uuid REFERENCES employees(id);
COMMENT ON COLUMN employee_training.chosen IS 'Qisman reja: tanlangan topshiriqlar kalitlari (NULL — roldagi barcha topshiriqlar)';
COMMENT ON COLUMN employee_training.previous IS 'Xodim o''zi boshlagan takroriy o''quvdan chiqsa qaytadigan holat (NULL — o''quv majburiy)';
COMMENT ON COLUMN employee_training.attempt IS 'Nechanchi marta o''qiyapti (1 — birinchi o''quv)';
COMMENT ON COLUMN employee_training.started_by IS 'Xodimni o''quvga kim qo''ygan: o''zi yoki rahbar (NULL — birinchi kirishda boshlangan)';
