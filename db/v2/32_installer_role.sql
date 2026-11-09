-- ============================================================================
-- TemoDB v2 · 32 · the "O‘rnatuvchi" (installer) role + admin.unit.reset
--   installer  = a system role that works like a superadmin, but only inside the units a superadmin gave the
--                employee: their viloyat (home unit), more viloyats of the same country (extra units) or the
--                whole country (home = country node). Never another country, never the platform keys.
--   admin.unit.reset = wipe a unit and everything under it ("like newborn"); superadmin + installer only.
-- Idempotent: re-running changes nothing.
-- ============================================================================

INSERT INTO roles (id, scope_unit_id, key, name, description, permissions, is_system) VALUES
('01a00000-0000-7000-8000-000000000007', NULL, 'installer', 'O‘rnatuvchi',
 'Hudud o''rnatuvchisi: o''z viloyati (yoki davlati) ichida superadmin kabi — birliklar, xodimlar, katalog, andozalar, sozlamalar, tozalash. Boshqa viloyat / davlatga chiqolmaydi; hududni superadmin beradi.',
 ARRAY['reception.patient.read','reception.patient.write','reception.order.create','reception.order.cancel','reception.order.delete',
       'reception.payment.create','reception.payment.refund','reception.payment.late_edit',
       'lab.worklist.read','lab.result.write','lab.result.submit',
       'confirm.result.read','confirm.result.view','confirm.result.approve','confirm.result.resend',
       'reports.finance.read','reports.finance.recent','reports.operations.read','reports.export',
       'messaging.send','messaging.broadcast',
       'admin.unit.read','admin.unit.write','admin.unit.manage',
       'admin.employee.read','admin.employee.write','admin.role.write',
       'admin.catalog.read','admin.catalog.write','admin.schema.write',
       'admin.template.read','admin.template.write','admin.template.publish',
       'admin.settings.write','admin.unit.reset'], true)
ON CONFLICT DO NOTHING;

-- the superadmin role lists every key (the flag bypasses checks anyway; the list is what the UI shows)
UPDATE roles
   SET permissions = array_append(permissions, 'admin.unit.reset'), updated_at = now()
 WHERE scope_unit_id IS NULL AND key = 'superadmin' AND deleted_at IS NULL
   AND NOT ('admin.unit.reset' = ANY(permissions));
