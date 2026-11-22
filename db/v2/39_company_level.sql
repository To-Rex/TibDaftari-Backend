-- ============================================================================
-- TemoDB v2 · 39 · the company — the root of the organisation tree
--
--   Kompaniya → Davlat → Viloyat → Tuman/Shahar → Muassasa(lar)
--
-- Until now a country node was the root. A company now stands above the countries: one company may work in several
-- countries, and several companies may work in the same country without seeing one another.
--
--   org_units.level       gains 'company' (the only level without a parent and without a territory)
--   org_units.company_id  the company every node belongs to (the company node: itself) — filled by the trigger
--   org_units.country_id  NULL for a company node only
--   one node per (company, country) / (company, region) / (company, district); codes are unique per company
--   patients.company_id   the registry is per company AND country: two companies never share a patient row
--   roles                 «Kompaniya rahbari» (leader_company) — the read-only leader of a whole company
--
-- Everything that exists today moves under ONE company («TibDaftari», rename it in the interface): the country
-- nodes get it as their parent, the closure table follows by its own trigger, nothing else changes — every id,
-- cheque number, setting, account and role stays where it was.
-- Idempotent: running it again changes nothing.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1 · columns
-- ---------------------------------------------------------------------------
ALTER TABLE org_units ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES org_units(id);
ALTER TABLE org_units ALTER COLUMN country_id DROP NOT NULL;
ALTER TABLE patients  ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES org_units(id);

-- the old shape rules go first: the rows are about to change shape
ALTER TABLE org_units
  DROP CONSTRAINT IF EXISTS ck_org_units_level,
  DROP CONSTRAINT IF EXISTS ck_org_units_parent,
  DROP CONSTRAINT IF EXISTS ck_org_units_geo,
  DROP CONSTRAINT IF EXISTS ck_org_units_company_self;
ALTER TABLE org_units ADD CONSTRAINT ck_org_units_level
  CHECK (level IN ('company', 'country', 'region', 'district', 'facility'));

-- ---------------------------------------------------------------------------
-- 2 · validation: nesting (company → country → region → district → facility), territory, the company of a node
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION org_units_validate() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  p        org_units%ROWTYPE;
  r_country uuid;
  d_region  uuid;
  expected  text;
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.level <> OLD.level THEN
    RAISE EXCEPTION 'org_units: level is immutable (% → %)', OLD.level, NEW.level USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.level = 'company' THEN
    -- the root: no parent, no territory (CHECK constraints), its own company
    NEW.company_id := NEW.id;
    IF NEW.timezone IS NULL THEN
      NEW.timezone := 'Asia/Tashkent';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.region_id IS NOT NULL THEN
    SELECT country_id INTO r_country FROM regions WHERE id = NEW.region_id;
    IF r_country IS DISTINCT FROM NEW.country_id THEN
      RAISE EXCEPTION 'org_units: region % does not belong to country %', NEW.region_id, NEW.country_id USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF NEW.district_id IS NOT NULL THEN
    SELECT region_id INTO d_region FROM districts WHERE id = NEW.district_id;
    IF d_region IS DISTINCT FROM NEW.region_id THEN
      RAISE EXCEPTION 'org_units: district % does not belong to region %', NEW.district_id, NEW.region_id USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  IF NEW.parent_id IS NOT NULL THEN
    SELECT * INTO p FROM org_units WHERE id = NEW.parent_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'org_units: parent % not found', NEW.parent_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    expected := CASE NEW.level WHEN 'country' THEN 'company' WHEN 'region' THEN 'country' WHEN 'district' THEN 'region' WHEN 'facility' THEN 'district' END;
    IF p.level <> expected THEN
      RAISE EXCEPTION 'org_units: a % must hang under a %, but parent % is a %', NEW.level, expected, p.id, p.level USING ERRCODE = 'check_violation';
    END IF;
    IF (p.country_id  IS NOT NULL AND p.country_id  IS DISTINCT FROM NEW.country_id)
       OR (p.region_id   IS NOT NULL AND p.region_id   IS DISTINCT FROM NEW.region_id)
       OR (p.district_id IS NOT NULL AND p.district_id IS DISTINCT FROM NEW.district_id) THEN
      RAISE EXCEPTION 'org_units: territory of % does not match its parent %', NEW.id, p.id USING ERRCODE = 'check_violation';
    END IF;
    -- a node lives in its parent's company — never chosen by hand, never changed afterwards
    NEW.company_id := p.company_id;
  END IF;
  IF TG_OP = 'UPDATE' AND OLD.company_id IS NOT NULL AND NEW.company_id IS DISTINCT FROM OLD.company_id THEN
    RAISE EXCEPTION 'org_units: % cannot move to another company (% → %)', NEW.id, OLD.company_id, NEW.company_id USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.timezone IS NULL THEN
    SELECT timezone INTO NEW.timezone FROM countries WHERE id = NEW.country_id;
  END IF;
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------
-- 3 · today's tree moves under one company
--     (updated_at is left alone: nothing about these rows changed for the people who use them)
-- ---------------------------------------------------------------------------
ALTER TABLE org_units DISABLE TRIGGER USER;  -- updated_at + validation: the rows are re-homed, not re-judged
ALTER TABLE org_units ENABLE TRIGGER trg_org_units_closure_insert;
ALTER TABLE org_units ENABLE TRIGGER trg_org_units_closure_move;
ALTER TABLE patients  DISABLE TRIGGER USER;

INSERT INTO org_units (id, company_id, level, parent_id, country_id, name, sort_order, timezone)
SELECT '01a1be00-0000-7000-8000-0000000000c0', '01a1be00-0000-7000-8000-0000000000c0', 'company', NULL, NULL, 'TibDaftari', 0, 'Asia/Tashkent'
WHERE NOT EXISTS (SELECT 1 FROM org_units WHERE level = 'company')
  AND (EXISTS (SELECT 1 FROM org_units WHERE level = 'country' AND parent_id IS NULL)
       OR EXISTS (SELECT 1 FROM patients WHERE company_id IS NULL));

-- country nodes: the company becomes their parent (the closure trigger links every subtree to it)
UPDATE org_units SET parent_id = (SELECT id FROM org_units WHERE level = 'company' ORDER BY created_at, id LIMIT 1)
WHERE level = 'country' AND parent_id IS NULL;

-- every node: the company at the top of its path
UPDATE org_units u SET company_id = c.ancestor_id
FROM org_unit_closure c JOIN org_units a ON a.id = c.ancestor_id AND a.level = 'company'
WHERE c.descendant_id = u.id AND u.company_id IS NULL;

-- patients: the company of the facility that registered them; the ones nobody registered (imports) go to the
-- company that works in their country, else to the first company
UPDATE patients p SET company_id = u.company_id
FROM org_units u WHERE u.id = p.registered_unit_id AND p.company_id IS NULL;
UPDATE patients p SET company_id = (
  SELECT u.company_id FROM org_units u
  WHERE u.level = 'country' AND u.country_id = p.country_id
  ORDER BY (u.deleted_at IS NOT NULL), u.created_at, u.id LIMIT 1)
WHERE p.company_id IS NULL;
UPDATE patients SET company_id = (SELECT id FROM org_units WHERE level = 'company' ORDER BY created_at, id LIMIT 1)
WHERE company_id IS NULL;

ALTER TABLE org_units ENABLE TRIGGER USER;
ALTER TABLE patients  ENABLE TRIGGER USER;

-- ---------------------------------------------------------------------------
-- 4 · the new shape rules
-- ---------------------------------------------------------------------------
ALTER TABLE org_units ALTER COLUMN company_id SET NOT NULL;
ALTER TABLE patients  ALTER COLUMN company_id SET NOT NULL;

ALTER TABLE org_units
  ADD CONSTRAINT ck_org_units_parent CHECK ((level = 'company') = (parent_id IS NULL)),
  ADD CONSTRAINT ck_org_units_geo CHECK (
       (level = 'company'  AND country_id IS NULL     AND region_id IS NULL     AND district_id IS NULL)
    OR (level = 'country'  AND country_id IS NOT NULL AND region_id IS NULL     AND district_id IS NULL)
    OR (level = 'region'   AND country_id IS NOT NULL AND region_id IS NOT NULL AND district_id IS NULL)
    OR (level IN ('district', 'facility') AND country_id IS NOT NULL AND region_id IS NOT NULL AND district_id IS NOT NULL)),
  ADD CONSTRAINT ck_org_units_company_self CHECK ((level = 'company') = (company_id = id));

-- one node per territory INSIDE a company; codes (cheque prefixes) are unique inside a company
DROP INDEX IF EXISTS uq_org_units_country_alive;
DROP INDEX IF EXISTS uq_org_units_region_alive;
DROP INDEX IF EXISTS uq_org_units_district_alive;
DROP INDEX IF EXISTS uq_org_units_code_alive;
CREATE UNIQUE INDEX uq_org_units_country_alive  ON org_units (company_id, country_id)  WHERE level = 'country'  AND deleted_at IS NULL;
CREATE UNIQUE INDEX uq_org_units_region_alive   ON org_units (company_id, region_id)   WHERE level = 'region'   AND deleted_at IS NULL;
CREATE UNIQUE INDEX uq_org_units_district_alive ON org_units (company_id, district_id) WHERE level = 'district' AND deleted_at IS NULL;
CREATE UNIQUE INDEX uq_org_units_code_alive     ON org_units (company_id, lower(code)) WHERE code IS NOT NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_org_units_company_name_alive ON org_units (lower(name)) WHERE level = 'company' AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_org_units_company ON org_units (company_id, level);

COMMENT ON TABLE  org_units            IS 'Tashkiliy daraxt: kompaniya → davlat → viloyat → tuman/shahar → muassasa(lar).';
COMMENT ON COLUMN org_units.level      IS 'company | country | region | district | facility';
COMMENT ON COLUMN org_units.company_id IS 'Tugun tegishli kompaniya (kompaniya tugunida — o''zi). Trigger ota tugundan oladi.';
COMMENT ON COLUMN org_units.country_id IS 'Davlat (faqat kompaniya tugunida NULL)';

-- ---------------------------------------------------------------------------
-- 5 · patients: one registry per company and country
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS uq_patients_country_passport_alive;
DROP INDEX IF EXISTS uq_patients_country_pinfl_alive;
DROP INDEX IF EXISTS ix_patients_country_phone;
DROP INDEX IF EXISTS ix_patients_country_created;
CREATE UNIQUE INDEX uq_patients_country_passport_alive ON patients (company_id, country_id, passport_number)
  WHERE deleted_at IS NULL AND passport_number IS NOT NULL;
CREATE UNIQUE INDEX uq_patients_country_pinfl_alive ON patients (company_id, country_id, pinfl)
  WHERE deleted_at IS NULL AND pinfl IS NOT NULL;
CREATE INDEX ix_patients_country_phone   ON patients (company_id, country_id, phone);
CREATE INDEX ix_patients_country_created ON patients (company_id, country_id, created_at);
CREATE INDEX IF NOT EXISTS ix_patients_phone ON patients (phone) WHERE deleted_at IS NULL;

-- a patient lives in the company of the facility that registered them
CREATE OR REPLACE FUNCTION patients_company() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE c uuid;
BEGIN
  IF NEW.registered_unit_id IS NOT NULL THEN
    SELECT company_id INTO c FROM org_units WHERE id = NEW.registered_unit_id;
    IF NEW.company_id IS NULL THEN
      NEW.company_id := c;
    ELSIF c IS DISTINCT FROM NEW.company_id THEN
      RAISE EXCEPTION 'patients: facility % belongs to another company than the patient (%)', NEW.registered_unit_id, NEW.company_id
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_patients_company ON patients;
CREATE TRIGGER trg_patients_company BEFORE INSERT OR UPDATE OF registered_unit_id, company_id ON patients
  FOR EACH ROW EXECUTE FUNCTION patients_company();

COMMENT ON TABLE  patients            IS 'Bemorlar — kompaniya va davlat bo''yicha yagona reyestr';
COMMENT ON COLUMN patients.company_id IS 'Bemor tegishli kompaniya (ro''yxatga olgan muassasaning kompaniyasi). Kompaniyalar bemorlarni bo''lishmaydi.';

-- ---------------------------------------------------------------------------
-- 6 · views: the company of every node, countries in the counters
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_org_tree AS
SELECT u.id, u.level, u.kind, u.name, u.short_name, u.code, u.is_active, u.parent_id, u.sort_order,
       u.country_id,  c.name AS country_name,
       u.region_id,   r.name AS region_name,
       u.district_id, d.name AS district_name, d.kind AS district_kind,
       (SELECT max(cl.depth) FROM org_unit_closure cl WHERE cl.descendant_id = u.id) AS depth,
       unit_path(u.id) AS path,
       u.timezone, u.deleted_at,
       u.company_id, co.name AS company_name
FROM org_units u
JOIN org_units co      ON co.id = u.company_id
LEFT JOIN countries c  ON c.id = u.country_id
LEFT JOIN regions r    ON r.id = u.region_id
LEFT JOIN districts d  ON d.id = u.district_id;
COMMENT ON VIEW v_org_tree IS 'org_units + kompaniya/davlat/viloyat/tuman nomlari, chuqurlik va to''liq yo''l';

CREATE OR REPLACE VIEW v_facilities AS
SELECT * FROM v_org_tree WHERE level = 'facility';

CREATE OR REPLACE VIEW v_org_unit_counts AS
SELECT a.ancestor_id AS unit_id,
       count(*) FILTER (WHERE u.level = 'region'   AND u.deleted_at IS NULL) AS regions,
       count(*) FILTER (WHERE u.level = 'district' AND u.deleted_at IS NULL) AS districts,
       count(*) FILTER (WHERE u.level = 'facility' AND u.deleted_at IS NULL) AS facilities,
       count(*) FILTER (WHERE u.level = 'facility' AND u.deleted_at IS NULL AND u.is_active) AS facilities_active,
       count(*) FILTER (WHERE u.level = 'country'  AND u.deleted_at IS NULL) AS countries
FROM org_unit_closure a
JOIN org_units u ON u.id = a.descendant_id AND a.depth > 0
GROUP BY a.ancestor_id;

COMMENT ON FUNCTION unit_path(uuid) IS 'TibDaftari › O‘zbekiston › Xorazm viloyati › Bog‘ot tumani › Bog‘ot tumani SES';
COMMENT ON FUNCTION effective_settings(uuid) IS 'Birlik uchun amaldagi sozlamalar: kompaniya → davlat → … → o''zi, yaqinroq daraja ustun';

-- ---------------------------------------------------------------------------
-- 7 · «Kompaniya rahbari» — the read-only leader of a whole company (the fifth leader role, see 36_)
-- ---------------------------------------------------------------------------
INSERT INTO roles (id, scope_unit_id, key, name, description, permissions, is_system) VALUES
('01a00000-0000-7000-8000-00000000000c', NULL, 'leader_company', 'Kompaniya rahbari',
 'Faqat ko''rish: butun kompaniya (barcha davlatlari, viloyatlari va muassasalari) bo''yicha hisobotlar, statistika, bemorlar, cheklar, natijalar, xodimlar, katalog va andozalar. Hech narsani o''zgartirmaydi. Kompaniya tuguniga biriktiriladi.',
 ARRAY['reception.patient.read','lab.worklist.read','confirm.result.read','confirm.result.view',
       'reports.finance.read','reports.operations.read','reports.export',
       'admin.unit.read','admin.employee.read','admin.catalog.read','admin.template.read','assistant.use'], true)
ON CONFLICT DO NOTHING;
