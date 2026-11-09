-- ============================================================================
-- TemoDB v2 · 09 · read views for the hierarchy and territory statistics
-- ============================================================================

-- the tree with its geo names, depth and full path
CREATE VIEW v_org_tree AS
SELECT u.id, u.level, u.kind, u.name, u.short_name, u.code, u.is_active, u.parent_id, u.sort_order,
       u.country_id,  c.name AS country_name,
       u.region_id,   r.name AS region_name,
       u.district_id, d.name AS district_name, d.kind AS district_kind,
       (SELECT max(cl.depth) FROM org_unit_closure cl WHERE cl.descendant_id = u.id) AS depth,
       unit_path(u.id) AS path,
       u.timezone, u.deleted_at
FROM org_units u
JOIN countries c       ON c.id = u.country_id
LEFT JOIN regions r    ON r.id = u.region_id
LEFT JOIN districts d  ON d.id = u.district_id;
COMMENT ON VIEW v_org_tree IS 'org_units + davlat/viloyat/tuman nomlari, chuqurlik va to''liq yo''l';

-- facilities only (the operating level)
CREATE VIEW v_facilities AS
SELECT * FROM v_org_tree WHERE level = 'facility';
COMMENT ON VIEW v_facilities IS 'Muassasalar (chek yoziladigan daraja) — tuman/viloyat/davlat nomlari bilan';

-- how many live children of each kind a unit has
CREATE VIEW v_org_unit_counts AS
SELECT a.ancestor_id AS unit_id,
       count(*) FILTER (WHERE u.level = 'region'   AND u.deleted_at IS NULL) AS regions,
       count(*) FILTER (WHERE u.level = 'district' AND u.deleted_at IS NULL) AS districts,
       count(*) FILTER (WHERE u.level = 'facility' AND u.deleted_at IS NULL) AS facilities,
       count(*) FILTER (WHERE u.level = 'facility' AND u.deleted_at IS NULL AND u.is_active) AS facilities_active
FROM org_unit_closure a
JOIN org_units u ON u.id = a.descendant_id AND a.depth > 0
GROUP BY a.ancestor_id;

-- daily activity per facility in the facility's local day; roll up to any level
-- with:  SELECT … FROM v_territory_daily t JOIN org_unit_closure c ON c.descendant_id = t.unit_id
--        WHERE c.ancestor_id = :region_unit GROUP BY …
CREATE VIEW v_territory_daily AS
WITH o AS (
  SELECT o.unit_id, (o.created_at AT TIME ZONE u.timezone)::date AS day, count(*)::bigint AS orders
  FROM orders o JOIN org_units u ON u.id = o.unit_id
  WHERE o.deleted_at IS NULL GROUP BY 1, 2),
p AS (
  SELECT p.unit_id, (p.created_at AT TIME ZONE u.timezone)::date AS day, sum(p.amount)::bigint AS revenue
  FROM payments p JOIN org_units u ON u.id = p.unit_id
  WHERE p.deleted_at IS NULL AND p.refunded_at IS NULL GROUP BY 1, 2),
n AS (
  SELECT pt.registered_unit_id AS unit_id, (pt.created_at AT TIME ZONE u.timezone)::date AS day, count(*)::bigint AS patients_new
  FROM patients pt JOIN org_units u ON u.id = pt.registered_unit_id
  WHERE pt.deleted_at IS NULL GROUP BY 1, 2),
d AS (
  SELECT rd.unit_id, (rd.created_at AT TIME ZONE u.timezone)::date AS day, count(*)::bigint AS results_ready
  FROM result_documents rd JOIN org_units u ON u.id = rd.unit_id
  WHERE rd.deleted_at IS NULL AND rd.status = 'final' GROUP BY 1, 2),
s AS (
  SELECT m.unit_id, (m.sent_at AT TIME ZONE u.timezone)::date AS day, count(*)::bigint AS sms_sent
  FROM outbox_messages m JOIN org_units u ON u.id = m.unit_id
  WHERE m.channel = 'sms' AND m.status IN ('sent', 'delivered') AND m.sent_at IS NOT NULL GROUP BY 1, 2)
SELECT unit_id, day,
       COALESCE(o.orders, 0)        AS orders,
       COALESCE(p.revenue, 0)       AS revenue,
       COALESCE(n.patients_new, 0)  AS patients_new,
       COALESCE(d.results_ready, 0) AS results_ready,
       COALESCE(s.sms_sent, 0)      AS sms_sent
FROM o
FULL JOIN p USING (unit_id, day)
FULL JOIN n USING (unit_id, day)
FULL JOIN d USING (unit_id, day)
FULL JOIN s USING (unit_id, day);
COMMENT ON VIEW v_territory_daily IS 'Muassasa × kun: cheklar, tushum, yangi bemorlar, tayyor natijalar, SMS. Hududga yig''ish org_unit_closure orqali.';
