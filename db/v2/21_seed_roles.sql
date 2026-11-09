-- ============================================================================
-- TemoDB v2 · 21 · system roles (scope_unit_id NULL = available everywhere)
-- Permission keys = app/core/permissions.py (35 keys). WHERE a role applies is
-- decided by the employee's unit, so the same "rahbar" role is a viloyat
-- rahbari when attached to a region node and a muassasa rahbari on a facility.
-- Fixed ids so the data migration could map the old per-organisation roles onto these.
-- ============================================================================

INSERT INTO roles (id, scope_unit_id, key, name, description, permissions, is_system) VALUES
('01a00000-0000-7000-8000-000000000001', NULL, 'superadmin', 'Superadmin',
 'Platforma egasi: barcha davlatlar, hudud daraxti, muassasalar va sozlamalar',
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
       'admin.settings.write','platform.unit.manage'], true),

('01a00000-0000-7000-8000-000000000002', NULL, 'admin', 'Administrator',
 'Birlik ma''muri: o''z birligi va ostidagi hamma narsa (platforma boshqaruvidan tashqari)',
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
       'admin.settings.write'], true),

('01a00000-0000-7000-8000-000000000003', NULL, 'registrator', 'Registrator / Kassir',
 'Qabulxona: bemor, chek, to''lov (1 kun ichida tahrir), tasdiqlangan natijalarni ko''rish va qayta yuborish',
 ARRAY['reception.patient.read','reception.patient.write','reception.order.create','reception.order.cancel','reception.order.delete',
       'reception.payment.create','reports.operations.read','messaging.send',
       'reports.finance.read','reports.export','reports.finance.recent',
       'confirm.result.view','confirm.result.resend'], true),

('01a00000-0000-7000-8000-000000000004', NULL, 'laborant', 'Laborant',
 'Laboratoriya: ish ro''yxati, natija kiritish va yuborish',
 ARRAY['lab.worklist.read','lab.result.write','lab.result.submit'], true),

('01a00000-0000-7000-8000-000000000005', NULL, 'vrach', 'Vrach',
 'Natijalarni tasdiqlash va qaytarish',
 ARRAY['lab.worklist.read','lab.result.write','confirm.result.read','confirm.result.approve','confirm.result.resend',
       'reception.patient.read'], true),

('01a00000-0000-7000-8000-000000000006', NULL, 'rahbar', 'Rahbar',
 'Hisobotlar (moliya, faoliyat, eksport), xodimlar ro''yxati, eski to''lovlarni tuzatish. Viloyat tuguniga biriktirilsa — viloyat rahbari.',
 ARRAY['reports.finance.read','reports.operations.read','reports.export','reception.patient.read','admin.employee.read',
       'reception.payment.late_edit'], true);
