-- ============================================================================
-- TemoDB v2 · 41 · mandatory training of a new employee («o‘quv rejimi»)
--   employee_training   where each employee stands: active (still learning — their requests work in the sandbox
--                       schema `training`, never in the real data), passed (did every task), skipped (let through
--                       by a manager), exempt (never needed it). `tasks` = task key → when it was done.
-- The sandbox schema itself (`training.*`: copies of the operational tables) is NOT created here — the backend
-- builds it at start from the live definitions of the real tables and rebuilds it whenever those change
-- (app/modules/training/sandbox.py), so no later migration has to remember it.
-- Everyone who has already worked in the system keeps working with the real data: they are marked `exempt`.
-- An account nobody has signed in to yet meets the training at its first sign-in.
-- Idempotent: re-running changes nothing.
-- ============================================================================

CREATE TABLE IF NOT EXISTS employee_training (
  employee_id  uuid         PRIMARY KEY REFERENCES employees(id) ON DELETE CASCADE,
  status       varchar(10)  NOT NULL DEFAULT 'active',
  tasks        jsonb        NOT NULL DEFAULT '{}'::jsonb,           -- { "<task key>": "<ISO time done>" | null }
  started_at   timestamptz  NOT NULL DEFAULT now(),
  completed_at timestamptz,
  completed_by uuid         REFERENCES employees(id),               -- the manager who let them through (skipped)
  note         varchar(40),                                         -- why exempt: existing | superadmin | installer | no_tasks
  created_at   timestamptz  NOT NULL DEFAULT now(),
  updated_at   timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT ck_employee_training_status CHECK (status IN ('active', 'passed', 'skipped', 'exempt'))
);
CREATE INDEX IF NOT EXISTS ix_employee_training_status ON employee_training (status) WHERE status = 'active';
COMMENT ON TABLE employee_training IS 'Xodimning majburiy o''quvi: active — o''quvda (so''rovlari training sxemasida ishlaydi), passed — yakunlagan, skipped — rahbar o''tkazib yuborgan, exempt — kerak bo''lmagan';

-- nobody who already works is sent back to school: any trace of having used the system counts (a sign-in, a
-- session, an audit line, a cheque made, a result entered or approved)
INSERT INTO employee_training (employee_id, status, note, completed_at)
SELECT e.id, 'exempt', CASE WHEN e.is_super_admin THEN 'superadmin' ELSE 'existing' END, now()
FROM employees e
WHERE e.is_super_admin
   OR e.last_login_at IS NOT NULL
   OR EXISTS (SELECT 1 FROM sessions s WHERE s.actor = 'staff' AND s.subject_id = e.id)
   OR EXISTS (SELECT 1 FROM audit_log a WHERE a.actor_type = 'staff' AND a.actor_id = e.id)
   OR EXISTS (SELECT 1 FROM orders o WHERE o.created_by_employee_id = e.id)
   OR EXISTS (SELECT 1 FROM order_items i WHERE i.technician_id = e.id OR i.doctor_id = e.id)
ON CONFLICT (employee_id) DO NOTHING;

SELECT attach_updated_at_triggers();
