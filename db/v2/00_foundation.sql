-- ============================================================================
-- TemoDB v2 · 00 · foundation
-- Extensions, shared helpers and the script registry.
-- Hierarchy of the whole system:  Davlat → Viloyat → Tuman/Shahar → Muassasa(lar)
-- There is NO company/tenant table any more: every operational row points at an
-- org_unit (usually a facility) and permissions are a subtree of that tree.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;

-- ---------------------------------------------------------------------------
-- script registry (what was applied, when, with which checksum)
-- ---------------------------------------------------------------------------
CREATE TABLE schema_migrations (
  version    text        PRIMARY KEY,
  name       text        NOT NULL,
  checksum   char(64)    NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE schema_migrations IS 'TemoDB v2 DDL/seed skriptlari: fayl versiyasi, nomi, sha256 va qo''llangan vaqti';

-- ---------------------------------------------------------------------------
-- fold_text — search folding, byte-for-byte the production function:
-- lower → Cyrillic→Latin → strip apostrophes → h→x → collapse whitespace
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fold_text(src text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
  SELECT btrim(regexp_replace(replace(regexp_replace(
    translate(
      replace(replace(replace(replace(replace(replace(replace(lower(src),
        'ё','yo'),'ц','ts'),'ч','ch'),'ш','sh'),'щ','sh'),'ю','yu'),'я','ya'),
      'абвгдежзийклмнопрстуфхыэўқғҳъь', 'abvgdejziyklmnoprstufxieoqgh'),
    '[‘’''`ʻ]', '', 'g'), 'h', 'x'), '\s+', ' ', 'g'))
$$;
COMMENT ON FUNCTION fold_text(text) IS 'Qidiruv uchun matnni soddalashtirish (kirill→lotin, apostroflarsiz, h→x). Trigram indekslar shu funksiya ustida.';

-- ---------------------------------------------------------------------------
-- updated_at guard: every table with an updated_at column gets this trigger
-- (attach_updated_at_triggers() is called at the end of each DDL script)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION attach_updated_at_triggers() RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE t record; n integer := 0;
BEGIN
  FOR t IN
    SELECT c.relname AS table_name
    FROM pg_class c
    JOIN pg_namespace ns ON ns.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'updated_at' AND NOT a.attisdropped
    WHERE ns.nspname = 'public' AND c.relkind IN ('r', 'p')
      AND NOT EXISTS (SELECT 1 FROM pg_trigger tg WHERE tg.tgrelid = c.oid AND tg.tgname = 'trg_' || c.relname || '_updated_at')
  LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION set_updated_at()',
                   'trg_' || t.table_name || '_updated_at', t.table_name);
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;
COMMENT ON FUNCTION attach_updated_at_triggers() IS 'updated_at ustuni bor har bir jadvalga BEFORE UPDATE triggerini qo''yadi (idempotent)';

-- ---------------------------------------------------------------------------
-- small shared domains
-- ---------------------------------------------------------------------------
CREATE DOMAIN money_uzs AS bigint
  CHECK (VALUE >= 0);
COMMENT ON DOMAIN money_uzs IS 'Pul — butun son (tiyinsiz), manfiy bo''lmaydi';

CREATE DOMAIN percent_0_100 AS smallint
  CHECK (VALUE BETWEEN 0 AND 100);
