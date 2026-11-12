-- ============================================================================
-- TemoDB v2 · 36 · the four read-only leader roles («rahbar»), one per level of the tree
--   leader_country  = Davlat rahbari   — homed on a country node
--   leader_region   = Viloyat rahbari  — homed on a region node
--   leader_district = Tuman rahbari    — homed on a district node
--   leader_facility = Muassasa rahbari — homed on a facility
-- They look at everything inside their units (reports, statistics, lists, cards, documents, the assistant's read
-- tools) and change nothing: only read / view / export keys (app/core/permissions.py LEADER_PERMISSIONS).
-- The level rule lives in the API (422 leader_scope). Idempotent: re-running changes nothing.
-- ============================================================================

INSERT INTO roles (id, scope_unit_id, key, name, description, permissions, is_system) VALUES
('01a00000-0000-7000-8000-000000000008', NULL, 'leader_country', 'Davlat rahbari',
 'Faqat ko''rish: butun davlat bo''yicha hisobotlar, statistika, bemorlar, cheklar, natijalar, xodimlar, katalog va andozalar. Hech narsani o''zgartirmaydi. Davlat tuguniga biriktiriladi.',
 ARRAY['reception.patient.read','lab.worklist.read','confirm.result.read','confirm.result.view',
       'reports.finance.read','reports.operations.read','reports.export',
       'admin.unit.read','admin.employee.read','admin.catalog.read','admin.template.read','assistant.use'], true),
('01a00000-0000-7000-8000-000000000009', NULL, 'leader_region', 'Viloyat rahbari',
 'Faqat ko''rish: viloyat (tumanlari va muassasalari) bo''yicha hisobotlar, statistika, bemorlar, cheklar, natijalar, xodimlar, katalog va andozalar. Hech narsani o''zgartirmaydi. Viloyat tuguniga biriktiriladi.',
 ARRAY['reception.patient.read','lab.worklist.read','confirm.result.read','confirm.result.view',
       'reports.finance.read','reports.operations.read','reports.export',
       'admin.unit.read','admin.employee.read','admin.catalog.read','admin.template.read','assistant.use'], true),
('01a00000-0000-7000-8000-00000000000a', NULL, 'leader_district', 'Tuman rahbari',
 'Faqat ko''rish: tuman / shahar (muassasalari) bo''yicha hisobotlar, statistika, bemorlar, cheklar, natijalar, xodimlar, katalog va andozalar. Hech narsani o''zgartirmaydi. Tuman tuguniga biriktiriladi.',
 ARRAY['reception.patient.read','lab.worklist.read','confirm.result.read','confirm.result.view',
       'reports.finance.read','reports.operations.read','reports.export',
       'admin.unit.read','admin.employee.read','admin.catalog.read','admin.template.read','assistant.use'], true),
('01a00000-0000-7000-8000-00000000000b', NULL, 'leader_facility', 'Muassasa rahbari',
 'Faqat ko''rish: o''z muassasasi bo''yicha hisobotlar, statistika, bemorlar, cheklar, natijalar, xodimlar, katalog va andozalar. Hech narsani o''zgartirmaydi. Muassasaga biriktiriladi.',
 ARRAY['reception.patient.read','lab.worklist.read','confirm.result.read','confirm.result.view',
       'reports.finance.read','reports.operations.read','reports.export',
       'admin.unit.read','admin.employee.read','admin.catalog.read','admin.template.read','assistant.use'], true)
ON CONFLICT DO NOTHING;
