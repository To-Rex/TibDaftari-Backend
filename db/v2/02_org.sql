-- ============================================================================
-- TemoDB v2 · 02 · organisation tree  (Davlat → Viloyat → Tuman/Shahar → Muassasa)
--
-- org_units is ONE table for every level of the tree:
--   level = country   : the national node of a country  (1 per country)
--   level = region    : a viloyat node                   (1 per region)
--   level = district  : a tuman / shahar node            (1 per district)
--   level = facility  : an operating unit — SES bo'limi, klinika, laboratoriya …
--                       (ANY NUMBER per district — "har bir tumanda bir nechta")
-- Operational rows (orders, payments, documents, employees …) reference a unit.
-- A staff member attached to a region node sees the whole region subtree.
-- org_unit_closure (ancestor, descendant, depth) is kept by triggers so that
-- "everything under X" is a single indexed join, never a recursive query.
-- ============================================================================

CREATE TABLE org_units (
  id           uuid         PRIMARY KEY DEFAULT uuidv7(),
  level        varchar(10)  NOT NULL,
  parent_id    uuid         REFERENCES org_units(id),
  country_id   uuid         NOT NULL REFERENCES countries(id),
  region_id    uuid         REFERENCES regions(id),
  district_id  uuid         REFERENCES districts(id),
  kind         varchar(20),                              -- facility only: ses | clinic | laboratory | …
  name         varchar(200) NOT NULL,
  short_name   varchar(80),
  code         varchar(12),                              -- facility: cheque prefix  (UR-000123); others optional
  legal_name   varchar(300),
  phone        varchar(40),
  email        varchar(200),
  address      text,
  timezone     varchar(64),                              -- NULL → davlatniki (trigger to'ldiradi)
  logo_file_id uuid,                                     -- FK → stored_files (quyida)
  is_active    boolean      NOT NULL DEFAULT true,
  sort_order   integer      NOT NULL DEFAULT 0,
  settings     jsonb        NOT NULL DEFAULT '{}'::jsonb, -- meros bo'ladigan sozlamalar (effective_settings)
  created_by   uuid,
  created_at   timestamptz  NOT NULL DEFAULT now(),
  updated_at   timestamptz  NOT NULL DEFAULT now(),
  deleted_at   timestamptz,
  deleted_by   uuid,
  CONSTRAINT ck_org_units_level        CHECK (level IN ('country', 'region', 'district', 'facility')),
  CONSTRAINT ck_org_units_parent       CHECK ((level = 'country') = (parent_id IS NULL)),
  CONSTRAINT ck_org_units_geo          CHECK (
       (level = 'country'  AND region_id IS NULL     AND district_id IS NULL)
    OR (level = 'region'   AND region_id IS NOT NULL AND district_id IS NULL)
    OR (level IN ('district', 'facility') AND region_id IS NOT NULL AND district_id IS NOT NULL)),
  CONSTRAINT ck_org_units_kind         CHECK ((level = 'facility') = (kind IS NOT NULL)),
  CONSTRAINT ck_org_units_kind_values  CHECK (kind IS NULL OR kind IN ('ses', 'clinic', 'laboratory', 'polyclinic', 'hospital', 'pharmacy', 'other')),
  CONSTRAINT ck_org_units_facility_code CHECK (level <> 'facility' OR code IS NOT NULL),
  CONSTRAINT ck_org_units_code_format  CHECK (code IS NULL OR code ~ '^[A-Za-z0-9][A-Za-z0-9-]{0,11}$'),
  CONSTRAINT ck_org_units_settings     CHECK (jsonb_typeof(settings) = 'object'),
  CONSTRAINT ck_org_units_not_own_parent CHECK (parent_id IS NULL OR parent_id <> id)
);
CREATE UNIQUE INDEX uq_org_units_country_alive  ON org_units (country_id)  WHERE level = 'country'  AND deleted_at IS NULL;
CREATE UNIQUE INDEX uq_org_units_region_alive   ON org_units (region_id)   WHERE level = 'region'   AND deleted_at IS NULL;
CREATE UNIQUE INDEX uq_org_units_district_alive ON org_units (district_id) WHERE level = 'district' AND deleted_at IS NULL;
CREATE UNIQUE INDEX uq_org_units_code_alive     ON org_units (lower(code)) WHERE code IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX ix_org_units_parent          ON org_units (parent_id);
CREATE INDEX ix_org_units_level_geo       ON org_units (level, country_id, region_id, district_id);
CREATE INDEX ix_org_units_district_alive  ON org_units (district_id) WHERE level = 'facility' AND deleted_at IS NULL;
CREATE INDEX ix_org_units_fold_name_trgm  ON org_units USING gin (fold_text(name) gin_trgm_ops);

COMMENT ON TABLE  org_units          IS 'Tashkiliy daraxt: davlat → viloyat → tuman/shahar → muassasa(lar). Kompaniya tushunchasi yo''q.';
COMMENT ON COLUMN org_units.level    IS 'country | region | district | facility';
COMMENT ON COLUMN org_units.kind     IS 'Faqat muassasa uchun: ses | clinic | laboratory | polyclinic | hospital | pharmacy | other';
COMMENT ON COLUMN org_units.code     IS 'Muassasa kodi — chek raqamining prefiksi (UR-000123). Tirik yozuvlar orasida takrorlanmaydi.';
COMMENT ON COLUMN org_units.settings IS 'Pastga meros bo''ladigan sozlamalar (documentFreeViewDays, locale, …). effective_settings(unit) yig''ib beradi.';

-- ---------------------------------------------------------------------------
-- closure table: every (ancestor, descendant) pair incl. self (depth 0)
-- ---------------------------------------------------------------------------
CREATE TABLE org_unit_closure (
  ancestor_id   uuid     NOT NULL REFERENCES org_units(id) ON DELETE CASCADE,
  descendant_id uuid     NOT NULL REFERENCES org_units(id) ON DELETE CASCADE,
  depth         smallint NOT NULL CHECK (depth >= 0),
  PRIMARY KEY (ancestor_id, descendant_id)
);
CREATE INDEX ix_org_unit_closure_desc ON org_unit_closure (descendant_id, depth);
COMMENT ON TABLE org_unit_closure IS 'Daraxt yopilmasi: (ajdod, avlod, chuqurlik). Triggerlar yuritadi — qo''lda yozilmaydi.';

-- ---------------------------------------------------------------------------
-- validation: geo nesting, parent level/territory, immutability of level
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
    expected := CASE NEW.level WHEN 'region' THEN 'country' WHEN 'district' THEN 'region' WHEN 'facility' THEN 'district' END;
    IF p.level <> expected THEN
      RAISE EXCEPTION 'org_units: a % must hang under a %, but parent % is a %', NEW.level, expected, p.id, p.level USING ERRCODE = 'check_violation';
    END IF;
    IF p.country_id <> NEW.country_id
       OR (p.region_id   IS NOT NULL AND p.region_id   <> NEW.region_id)
       OR (p.district_id IS NOT NULL AND p.district_id <> NEW.district_id) THEN
      RAISE EXCEPTION 'org_units: territory of % does not match its parent %', NEW.id, p.id USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  IF NEW.timezone IS NULL THEN
    SELECT timezone INTO NEW.timezone FROM countries WHERE id = NEW.country_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_org_units_validate BEFORE INSERT OR UPDATE ON org_units
  FOR EACH ROW EXECUTE FUNCTION org_units_validate();

-- ---------------------------------------------------------------------------
-- closure maintenance
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION org_units_closure_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO org_unit_closure (ancestor_id, descendant_id, depth) VALUES (NEW.id, NEW.id, 0);
  IF NEW.parent_id IS NOT NULL THEN
    INSERT INTO org_unit_closure (ancestor_id, descendant_id, depth)
    SELECT c.ancestor_id, NEW.id, c.depth + 1 FROM org_unit_closure c WHERE c.descendant_id = NEW.parent_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_org_units_closure_insert AFTER INSERT ON org_units
  FOR EACH ROW EXECUTE FUNCTION org_units_closure_insert();

CREATE OR REPLACE FUNCTION org_units_closure_move() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.parent_id IS NOT DISTINCT FROM OLD.parent_id THEN
    RETURN NEW;
  END IF;
  IF NEW.parent_id IS NOT NULL AND EXISTS (
       SELECT 1 FROM org_unit_closure WHERE ancestor_id = NEW.id AND descendant_id = NEW.parent_id) THEN
    RAISE EXCEPTION 'org_units: cannot move % under its own descendant %', NEW.id, NEW.parent_id USING ERRCODE = 'check_violation';
  END IF;
  -- detach the subtree from the old ancestors (links inside the subtree stay)
  DELETE FROM org_unit_closure c
  USING org_unit_closure sub
  WHERE sub.ancestor_id = NEW.id
    AND c.descendant_id = sub.descendant_id
    AND c.ancestor_id IN (SELECT ancestor_id FROM org_unit_closure WHERE descendant_id = NEW.id AND ancestor_id <> NEW.id);
  -- attach it under the new ancestors
  IF NEW.parent_id IS NOT NULL THEN
    INSERT INTO org_unit_closure (ancestor_id, descendant_id, depth)
    SELECT a.ancestor_id, s.descendant_id, a.depth + s.depth + 1
    FROM org_unit_closure a
    JOIN org_unit_closure s ON s.ancestor_id = NEW.id
    WHERE a.descendant_id = NEW.parent_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_org_units_closure_move AFTER UPDATE OF parent_id ON org_units
  FOR EACH ROW EXECUTE FUNCTION org_units_closure_move();

-- ---------------------------------------------------------------------------
-- tree helpers
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION unit_descendants(root uuid) RETURNS SETOF uuid
LANGUAGE sql STABLE AS $$
  SELECT descendant_id FROM org_unit_closure WHERE ancestor_id = root
$$;
COMMENT ON FUNCTION unit_descendants(uuid) IS 'root va uning ostidagi barcha birliklar (ruxsat ko''lami)';

CREATE OR REPLACE FUNCTION unit_ancestors(leaf uuid) RETURNS TABLE (unit_id uuid, depth smallint)
LANGUAGE sql STABLE AS $$
  SELECT ancestor_id, depth FROM org_unit_closure WHERE descendant_id = leaf ORDER BY depth
$$;

CREATE OR REPLACE FUNCTION unit_in_scope(unit uuid, scope uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM org_unit_closure WHERE ancestor_id = scope AND descendant_id = unit)
$$;
COMMENT ON FUNCTION unit_in_scope(uuid, uuid) IS 'unit scope ichidami (scope = unit bo''lsa ham true)';

CREATE OR REPLACE FUNCTION unit_path(unit uuid) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT string_agg(u.name, ' › ' ORDER BY c.depth DESC)
  FROM org_unit_closure c JOIN org_units u ON u.id = c.ancestor_id
  WHERE c.descendant_id = unit
$$;
COMMENT ON FUNCTION unit_path(uuid) IS 'O‘zbekiston › Xorazm viloyati › Bog‘ot tumani › Bog‘ot tumani SES';

-- inherited settings: root → leaf shallow merge, the nearest level wins
CREATE OR REPLACE FUNCTION effective_settings(unit uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE acc jsonb := '{}'::jsonb; s jsonb;
BEGIN
  FOR s IN
    SELECT u.settings FROM org_unit_closure c JOIN org_units u ON u.id = c.ancestor_id
    WHERE c.descendant_id = unit ORDER BY c.depth DESC
  LOOP
    acc := acc || s;
  END LOOP;
  RETURN acc;
END $$;
COMMENT ON FUNCTION effective_settings(uuid) IS 'Birlik uchun amaldagi sozlamalar: davlat → … → o''zi, yaqinroq daraja ustun';

CREATE OR REPLACE FUNCTION effective_setting(unit uuid, key text) RETURNS jsonb
LANGUAGE sql STABLE AS $$
  SELECT u.settings -> key
  FROM org_unit_closure c JOIN org_units u ON u.id = c.ancestor_id
  WHERE c.descendant_id = unit AND u.settings ? key
  ORDER BY c.depth LIMIT 1
$$;

-- generic guard used by FK columns that must point at a specific level
--   CREATE TRIGGER … EXECUTE FUNCTION assert_unit_level('unit_id', 'facility')
CREATE OR REPLACE FUNCTION assert_unit_level() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v uuid; lv text;
BEGIN
  EXECUTE format('SELECT ($1).%I', TG_ARGV[0]) INTO v USING NEW;
  IF v IS NULL THEN RETURN NEW; END IF;
  SELECT level INTO lv FROM org_units WHERE id = v;
  IF lv IS DISTINCT FROM TG_ARGV[1] THEN
    RAISE EXCEPTION '%.%: unit % is a %, expected a %', TG_TABLE_NAME, TG_ARGV[0], v, COALESCE(lv, 'missing unit'), TG_ARGV[1]
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------
-- cheque numbering — one counter per facility, atomic
-- ---------------------------------------------------------------------------
CREATE TABLE order_counters (
  unit_id    uuid    PRIMARY KEY REFERENCES org_units(id),
  seq        integer NOT NULL DEFAULT 0 CHECK (seq >= 0),
  updated_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE order_counters IS 'Muassasa bo''yicha chek raqami hisoblagichi (next_order_number)';

CREATE OR REPLACE FUNCTION next_order_number(facility uuid) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE c text; n integer;
BEGIN
  SELECT code INTO c FROM org_units WHERE id = facility AND level = 'facility';
  IF c IS NULL THEN
    RAISE EXCEPTION 'next_order_number: % is not a facility', facility USING ERRCODE = 'check_violation';
  END IF;
  INSERT INTO order_counters (unit_id, seq) VALUES (facility, 1)
  ON CONFLICT (unit_id) DO UPDATE SET seq = order_counters.seq + 1, updated_at = now()
  RETURNING seq INTO n;
  RETURN format('%s-%s', c, lpad(n::text, 6, '0'));
END $$;
COMMENT ON FUNCTION next_order_number(uuid) IS 'Keyingi chek raqami: <muassasa kodi>-000123 (atomar)';

-- ---------------------------------------------------------------------------
-- stored_files — binary blobs (assets, PDFs). Kept in Postgres as today, with
-- room for an external object store later (storage = external + object_key).
-- ---------------------------------------------------------------------------
CREATE TABLE stored_files (
  id         uuid         PRIMARY KEY DEFAULT uuidv7(),
  unit_id    uuid         REFERENCES org_units(id),         -- NULL = tizim darajasidagi fayl
  sha256     char(64)     NOT NULL,
  mime       varchar(100) NOT NULL,
  size       integer      NOT NULL CHECK (size >= 0),
  filename   varchar(200),
  storage    varchar(10)  NOT NULL DEFAULT 'db',
  data       bytea,
  object_key text,
  is_public  boolean      NOT NULL DEFAULT false,
  created_at timestamptz  NOT NULL DEFAULT now(),
  created_by uuid,
  CONSTRAINT ck_stored_files_storage CHECK (storage IN ('db', 'external')),
  CONSTRAINT ck_stored_files_payload CHECK (
       (storage = 'db'       AND data IS NOT NULL)
    OR (storage = 'external' AND object_key IS NOT NULL))
);
CREATE INDEX ix_stored_files_unit_sha ON stored_files (unit_id, sha256);
COMMENT ON TABLE stored_files IS 'Fayllar (logo, muhr, imzo, natija PDF). storage=db → data ustunida; external → object_key.';

ALTER TABLE org_units ADD CONSTRAINT fk_org_units_logo FOREIGN KEY (logo_file_id) REFERENCES stored_files(id);

SELECT attach_updated_at_triggers();
