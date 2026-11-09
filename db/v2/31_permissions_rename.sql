-- ============================================================================
-- TemoDB v2 · 31 · permission keys renamed with the unit model
--   admin.company.read      → admin.unit.read
--   admin.company.write     → admin.unit.write
--   admin.branch.write      → admin.unit.manage
--   platform.company.manage → platform.unit.manage
-- Roles and employee overrides saved with the old names are rewritten in place (order preserved).
-- The backend also maps old names at read time (app/core/permissions.py RENAMED), so this is data hygiene.
-- ============================================================================

CREATE OR REPLACE FUNCTION pg_temp.rename_perm(p text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE p
    WHEN 'admin.company.read'      THEN 'admin.unit.read'
    WHEN 'admin.company.write'     THEN 'admin.unit.write'
    WHEN 'admin.branch.write'      THEN 'admin.unit.manage'
    WHEN 'platform.company.manage' THEN 'platform.unit.manage'
    ELSE p END
$$;

UPDATE roles r
   SET permissions = sub.perms
  FROM (SELECT ro.id, array_agg(pg_temp.rename_perm(u.p) ORDER BY u.ord) AS perms
          FROM roles ro, unnest(ro.permissions) WITH ORDINALITY AS u(p, ord)
         GROUP BY ro.id) sub
 WHERE sub.id = r.id
   AND r.permissions && ARRAY['admin.company.read', 'admin.company.write', 'admin.branch.write', 'platform.company.manage']::text[];

UPDATE employees
   SET overrides = jsonb_build_object(
         'allow', COALESCE((SELECT jsonb_agg(pg_temp.rename_perm(v)) FROM jsonb_array_elements_text(COALESCE(overrides -> 'allow', '[]'::jsonb)) v), '[]'::jsonb),
         'deny',  COALESCE((SELECT jsonb_agg(pg_temp.rename_perm(v)) FROM jsonb_array_elements_text(COALESCE(overrides -> 'deny',  '[]'::jsonb)) v), '[]'::jsonb))
 WHERE overrides::text LIKE '%admin.company.%' OR overrides::text LIKE '%admin.branch.write%' OR overrides::text LIKE '%platform.company.manage%';
