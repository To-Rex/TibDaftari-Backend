-- ============================================================================
-- TemoDB v2 · 22 · organisation tree for the data that exists today
--
--   O‘zbekiston Respublikasi                       (country node)
--   ├─ Xorazm viloyati                              (region node)   ← was the company "Xorazm SES"
--   │   ├─ Urganch shahri      → Xorazm viloyati SES boshqarmasi   (UR)
--   │   ├─ Bog‘ot tumani       → Bog‘ot tumani SES                 (BGT)
--   │   ├─ Gurlan tumani       → Gurlan tumani SES                 (GUR)
--   │   ├─ Hazorasp tumani     → Hazorasp tumani SES               (XZ)
--   │   ├─ Qo‘shko‘pir tumani  → Qo‘shko‘pir tumani SES            (QK)
--   │   ├─ Xonqa tumani        → Xonqa tumani SES                  (XQ)
--   │   └─ Tuproqqal‘a tumani  → Tuproqqal‘a tumani SES            (TQ)
--   └─ Toshkent shahri                               (region node)
--       └─ Yunusobod tumani    → Nur Klinika                       (YU)
--
-- Facility ids are the production branch ids, so migrating the data later is
-- "orders.branch_id → orders.unit_id" with no re-keying. Cheque counters keep
-- their current values, numbering continues (QK-000185, …). Geo ids are the
-- production reference ids. Structure only — no patients, orders or staff.
-- ============================================================================

-- country node
INSERT INTO org_units (id, level, parent_id, country_id, name, short_name, code, sort_order)
VALUES ('01a1be00-0000-7000-8000-000000000001', 'country', NULL,
        '01a0b069-7749-7d10-87b0-703892974a40', 'O‘zbekiston Respublikasi', 'O‘zbekiston', 'UZ', 1);

-- region nodes
INSERT INTO org_units (id, level, parent_id, country_id, region_id, name, code, sort_order) VALUES
('01a1be00-0000-7000-8000-000000000002', 'region', '01a1be00-0000-7000-8000-000000000001',
 '01a0b069-7749-7d10-87b0-703892974a40', '01a00cdd-5629-7c75-9ba6-16755c8680d8', 'Xorazm viloyati', 'XOR', 6),
('01a1be00-0000-7000-8000-000000000003', 'region', '01a1be00-0000-7000-8000-000000000001',
 '01a0b069-7749-7d10-87b0-703892974a40', '01a00cdd-5629-7c7d-bd34-ac6f6b647b9c', 'Toshkent shahri', 'TSH', 14);

-- district nodes (names come from the reference table)
INSERT INTO org_units (id, level, parent_id, country_id, region_id, district_id, name, sort_order)
SELECT x.id, 'district', x.parent_id, '01a0b069-7749-7d10-87b0-703892974a40', d.region_id, d.id, d.name, d.sort_order
FROM (VALUES
  ('01a1be00-0000-7000-8000-000000000011'::uuid, '01a1be00-0000-7000-8000-000000000002'::uuid, '01a00cdd-5828-7d57-8b4f-56625ae76290'::uuid), -- Urganch shahri
  ('01a1be00-0000-7000-8000-000000000012', '01a1be00-0000-7000-8000-000000000002', '01a00cdd-5828-7d59-9aa2-1b1dbd872c5f'), -- Bog‘ot
  ('01a1be00-0000-7000-8000-000000000013', '01a1be00-0000-7000-8000-000000000002', '01a00cdd-5828-7d5a-8657-3da04e563026'), -- Gurlan
  ('01a1be00-0000-7000-8000-000000000014', '01a1be00-0000-7000-8000-000000000002', '01a00cdd-5828-7d5b-b36f-be3011fe5a52'), -- Hazorasp
  ('01a1be00-0000-7000-8000-000000000015', '01a1be00-0000-7000-8000-000000000002', '01a00cdd-5828-7d5c-b4f3-ed5eab220546'), -- Qo‘shko‘pir
  ('01a1be00-0000-7000-8000-000000000016', '01a1be00-0000-7000-8000-000000000002', '01a00cdd-5828-7d60-95e0-b9f04473fc91'), -- Xonqa
  ('01a1be00-0000-7000-8000-000000000017', '01a1be00-0000-7000-8000-000000000002', '01a00cdd-5828-7d63-a883-ac4035109cfc'), -- Tuproqqal‘a
  ('01a1be00-0000-7000-8000-000000000021', '01a1be00-0000-7000-8000-000000000003', '01a00cdd-5828-7dd6-ad8f-ab70ac37c888')  -- Yunusobod
) AS x(id, parent_id, district_id)
JOIN districts d ON d.id = x.district_id;

-- facilities (ids = production branch ids; names cleaned, contacts kept)
INSERT INTO org_units (id, level, parent_id, country_id, region_id, district_id, kind, name, short_name, code, phone, address, timezone, created_at) VALUES
('01a00cdd-6ec6-799e-9453-6a00e639d0d3', 'facility', '01a1be00-0000-7000-8000-000000000011',
 '01a0b069-7749-7d10-87b0-703892974a40', '01a00cdd-5629-7c75-9ba6-16755c8680d8', '01a00cdd-5828-7d57-8b4f-56625ae76290',
 'ses', 'Xorazm viloyati SES boshqarmasi', 'Xorazm SES', 'UR', '+998 62 228-82-81', 'Urganch sh., A. Bahodirxon 177', 'Asia/Tashkent', '2025-07-13 04:00:00+00'),
('01a161fc-d825-795f-8837-6c4c56a05324', 'facility', '01a1be00-0000-7000-8000-000000000012',
 '01a0b069-7749-7d10-87b0-703892974a40', '01a00cdd-5629-7c75-9ba6-16755c8680d8', '01a00cdd-5828-7d59-9aa2-1b1dbd872c5f',
 'ses', 'Bog‘ot tumani SES', 'Bog‘ot SES', 'BGT', '+998 62 31-51-818', 'Bog‘ot tumani, Oqtepa mahallasi, Toshkent ko‘chasi, 38-uy', 'Asia/Tashkent', '2026-10-22 05:02:02+00'),
('01a1674a-cae1-7879-acce-73e9a9b6ea9f', 'facility', '01a1be00-0000-7000-8000-000000000013',
 '01a0b069-7749-7d10-87b0-703892974a40', '01a00cdd-5629-7c75-9ba6-16755c8680d8', '01a00cdd-5828-7d5a-8657-3da04e563026',
 'ses', 'Gurlan tumani SES', 'Gurlan SES', 'GUR', '+998 62 36-51-915', 'Gurlan tumani, Bobur ko‘chasi, 10-uy', 'Asia/Tashkent', '2026-10-23 05:45:17+00'),
('01a024d5-0a5d-706c-ac12-7824ab37c848', 'facility', '01a1be00-0000-7000-8000-000000000014',
 '01a0b069-7749-7d10-87b0-703892974a40', '01a00cdd-5629-7c75-9ba6-16755c8680d8', '01a00cdd-5828-7d5b-b36f-be3011fe5a52',
 'ses', 'Hazorasp tumani SES', 'Hazorasp SES', 'XZ', '+998 62 33-25-597', 'Hazorasp tumani, P. Mahmud mahallasi, G‘afur G‘ulom ko‘chasi, 51-uy', 'Asia/Tashkent', '2026-08-21 14:58:56+00'),
('01a15969-05ac-788b-92ae-212ee889f4b5', 'facility', '01a1be00-0000-7000-8000-000000000015',
 '01a0b069-7749-7d10-87b0-703892974a40', '01a00cdd-5629-7c75-9ba6-16755c8680d8', '01a00cdd-5828-7d5c-b4f3-ed5eab220546',
 'ses', 'Qo‘shko‘pir tumani SES', 'Qo‘shko‘pir SES', 'QK', '+998 62 26-71-02', 'Qo‘shko‘pir tumani, Beruniy ko‘chasi, 168-uy', 'Asia/Tashkent', '2026-10-20 13:03:37+00'),
('01a15971-3b7e-7fa3-92a3-711171ab1f00', 'facility', '01a1be00-0000-7000-8000-000000000016',
 '01a0b069-7749-7d10-87b0-703892974a40', '01a00cdd-5629-7c75-9ba6-16755c8680d8', '01a00cdd-5828-7d60-95e0-b9f04473fc91',
 'ses', 'Xonqa tumani SES', 'Xonqa SES', 'XQ', '+998 62 39-93-592', 'Xonqa tumani, Nurli osmon ko‘chasi, 34-uy', 'Asia/Tashkent', '2026-10-20 13:12:35+00'),
('01a00cdd-6ec6-799f-bc61-52cd12243a7f', 'facility', '01a1be00-0000-7000-8000-000000000017',
 '01a0b069-7749-7d10-87b0-703892974a40', '01a00cdd-5629-7c75-9ba6-16755c8680d8', '01a00cdd-5828-7d63-a883-ac4035109cfc',
 'ses', 'Tuproqqal‘a tumani SES', 'Tuproqqal‘a SES', 'TQ', '+998 62 305-38-36', 'Tuproqqal‘a tumani, Yoshlik mahallasi, Xorazm ko‘chasi, 2-uy', 'Asia/Tashkent', '2026-01-29 04:00:00+00'),
('01a00cdd-6ec6-79a0-863e-b562c9444189', 'facility', '01a1be00-0000-7000-8000-000000000021',
 '01a0b069-7749-7d10-87b0-703892974a40', '01a00cdd-5629-7c7d-bd34-ac6f6b647b9c', '01a00cdd-5828-7dd6-ad8f-ab70ac37c888',
 'clinic', 'Nur Klinika', 'Nur Klinika', 'YU', NULL, 'Toshkent sh., Yunusobod tumani', 'Asia/Tashkent', '2026-05-19 04:00:00+00');

-- cheque counters continue from today's values
INSERT INTO order_counters (unit_id, seq) VALUES
('01a00cdd-6ec6-799e-9453-6a00e639d0d3',  13),   -- UR
('01a161fc-d825-795f-8837-6c4c56a05324', 139),   -- BGT
('01a1674a-cae1-7879-acce-73e9a9b6ea9f',  33),   -- GUR
('01a024d5-0a5d-706c-ac12-7824ab37c848',  35),   -- XZ
('01a15969-05ac-788b-92ae-212ee889f4b5', 184),   -- QK
('01a15971-3b7e-7fa3-92a3-711171ab1f00',  25),   -- XQ
('01a00cdd-6ec6-799f-bc61-52cd12243a7f',  68),   -- TQ
('01a00cdd-6ec6-79a0-863e-b562c9444189',  88);   -- YU
