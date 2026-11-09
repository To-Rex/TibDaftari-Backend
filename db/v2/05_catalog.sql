-- ============================================================================
-- TemoDB v2 · 05 · catalogue: categories → services → schemas; templates; assets
--
-- Every catalogue row has an owner_unit_id — the level that defines it:
--   country node  → milliy katalog (hamma muassasa ko'radi)
--   region node   → viloyatga xos xizmatlar
--   facility      → faqat shu muassasaniki
-- A unit sees an item when the owner is itself or one of its ancestors
-- (catalog_visible). Per-unit price / on-off overrides live in
-- service_unit_settings and apply to the override unit's whole subtree,
-- nearest level wins (replaces branch_prices JSON + disabled_branch_ids).
-- ============================================================================

CREATE TABLE categories (
  id            uuid         PRIMARY KEY DEFAULT uuidv7(),
  owner_unit_id uuid         NOT NULL REFERENCES org_units(id),
  parent_id     uuid         REFERENCES categories(id),
  name          varchar(200) NOT NULL,
  code          varchar(40),
  icon          varchar(60),
  color         varchar(20),
  sort_order    integer      NOT NULL DEFAULT 0,
  is_active     boolean      NOT NULL DEFAULT true,
  phone         varchar(40),
  workflow      varchar(20)  NOT NULL DEFAULT 'lab',
  created_by    uuid,
  created_at    timestamptz  NOT NULL DEFAULT now(),
  updated_at    timestamptz  NOT NULL DEFAULT now(),
  deleted_at    timestamptz,
  deleted_by    uuid,
  CONSTRAINT ck_categories_not_own_parent CHECK (parent_id IS NULL OR parent_id <> id)
);
CREATE INDEX ix_categories_owner_order ON categories (owner_unit_id, sort_order);
CREATE INDEX ix_categories_parent      ON categories (parent_id);
COMMENT ON TABLE  categories               IS 'Xizmat kategoriyalari (daraxt). owner_unit_id = kim belgilagan (davlat/viloyat/muassasa).';
COMMENT ON COLUMN categories.workflow      IS 'lab = laboratoriya oqimi (natija kiritish → yuborish → tasdiqlash)';

CREATE TABLE attribute_schemas (
  id            uuid         PRIMARY KEY DEFAULT uuidv7(),
  owner_unit_id uuid         NOT NULL REFERENCES org_units(id),
  name          varchar(200) NOT NULL,
  description   text,
  version       integer      NOT NULL DEFAULT 1 CHECK (version >= 1),
  status        varchar(12)  NOT NULL DEFAULT 'draft',
  fields        jsonb        NOT NULL DEFAULT '[]'::jsonb,
  created_by    uuid,
  created_at    timestamptz  NOT NULL DEFAULT now(),
  updated_at    timestamptz  NOT NULL DEFAULT now(),
  deleted_at    timestamptz,
  deleted_by    uuid,
  CONSTRAINT ck_attribute_schemas_status CHECK (status IN ('draft', 'published', 'archived')),
  CONSTRAINT ck_attribute_schemas_fields CHECK (jsonb_typeof(fields) = 'array')
);
CREATE INDEX ix_attribute_schemas_owner ON attribute_schemas (owner_unit_id, status);
COMMENT ON TABLE attribute_schemas IS 'Natija maydonlari sxemalari (dinamik forma): fields = [{key, label, type, …}]';

CREATE TABLE result_templates (
  id               uuid         PRIMARY KEY DEFAULT uuidv7(),
  owner_unit_id    uuid         NOT NULL REFERENCES org_units(id),
  name             varchar(200) NOT NULL,
  description      text,
  status           varchar(12)  NOT NULL DEFAULT 'draft',
  version          integer      NOT NULL DEFAULT 1 CHECK (version >= 1),
  service_type_ids uuid[]       NOT NULL DEFAULT '{}',
  category_ids     uuid[]       NOT NULL DEFAULT '{}',
  scope            varchar(10)  NOT NULL DEFAULT 'item',
  language         varchar(2)   NOT NULL DEFAULT 'uz',
  doc              jsonb        NOT NULL DEFAULT '{}'::jsonb,
  thumbnail_url    text,
  usage            integer      NOT NULL DEFAULT 0 CHECK (usage >= 0),
  created_by       uuid,
  created_at       timestamptz  NOT NULL DEFAULT now(),
  updated_at       timestamptz  NOT NULL DEFAULT now(),
  deleted_at       timestamptz,
  deleted_by       uuid,
  CONSTRAINT ck_result_templates_status CHECK (status IN ('draft', 'active', 'archived')),
  CONSTRAINT ck_result_templates_scope  CHECK (scope IN ('item', 'order', 'receipt'))
);
CREATE INDEX ix_result_templates_owner_status ON result_templates (owner_unit_id, status);
CREATE INDEX ix_result_templates_services     ON result_templates USING gin (service_type_ids);
CREATE INDEX ix_result_templates_categories   ON result_templates USING gin (category_ids);
COMMENT ON TABLE  result_templates       IS 'Hujjat shablonlari (natija, chek darajasidagi hujjat, kvitansiya)';
COMMENT ON COLUMN result_templates.scope IS 'item = bitta tahlil, order = butun chek, receipt = kassa cheki';

-- a template limited to some units (empty = the owner's whole subtree)
CREATE TABLE template_units (
  template_id uuid NOT NULL REFERENCES result_templates(id) ON DELETE CASCADE,
  unit_id     uuid NOT NULL REFERENCES org_units(id),
  PRIMARY KEY (template_id, unit_id)
);
CREATE INDEX ix_template_units_unit ON template_units (unit_id);
COMMENT ON TABLE template_units IS 'Shablon faqat shu birliklarda (va ostida) ishlatiladi; yozuv bo''lmasa — egasining butun daraxtida';

CREATE TABLE service_types (
  id                  uuid         PRIMARY KEY DEFAULT uuidv7(),
  owner_unit_id       uuid         NOT NULL REFERENCES org_units(id),
  category_id         uuid         NOT NULL REFERENCES categories(id),
  name                varchar(300) NOT NULL,
  code                varchar(40),
  description         text,
  price               money_uzs    NOT NULL DEFAULT 0,          -- asosiy narx; birlik bo'yicha o'zgarishi service_unit_settings da
  turnaround_days     smallint     NOT NULL DEFAULT 1 CHECK (turnaround_days >= 0),
  sort_order          integer      NOT NULL DEFAULT 0,
  is_active           boolean      NOT NULL DEFAULT true,
  schema_id           uuid         REFERENCES attribute_schemas(id),
  document_scope      varchar(10)  NOT NULL DEFAULT 'item',
  default_template_id uuid         REFERENCES result_templates(id),
  created_by          uuid,
  created_at          timestamptz  NOT NULL DEFAULT now(),
  updated_at          timestamptz  NOT NULL DEFAULT now(),
  deleted_at          timestamptz,
  deleted_by          uuid,
  CONSTRAINT ck_service_types_document_scope CHECK (document_scope IN ('item', 'order'))
);
CREATE UNIQUE INDEX uq_service_types_owner_code_alive ON service_types (owner_unit_id, lower(code))
  WHERE deleted_at IS NULL AND code IS NOT NULL;
CREATE INDEX ix_service_types_owner_category ON service_types (owner_unit_id, category_id);
CREATE INDEX ix_service_types_category       ON service_types (category_id);
CREATE INDEX ix_service_types_schema         ON service_types (schema_id);
CREATE INDEX ix_service_types_fold_name_trgm ON service_types USING gin (fold_text(name) gin_trgm_ops);
COMMENT ON TABLE service_types IS 'Xizmatlar (tahlillar). Narx va yoqilgan/o''chirilganlik birlik bo''yicha service_unit_settings orqali o''zgaradi.';

CREATE TABLE service_unit_settings (
  service_type_id uuid        NOT NULL REFERENCES service_types(id) ON DELETE CASCADE,
  unit_id         uuid        NOT NULL REFERENCES org_units(id),
  is_enabled      boolean     NOT NULL DEFAULT true,
  price           bigint      CHECK (price IS NULL OR price >= 0),    -- NULL = asosiy narx
  updated_at      timestamptz NOT NULL DEFAULT now(),
  updated_by      uuid,
  PRIMARY KEY (service_type_id, unit_id)
);
CREATE INDEX ix_service_unit_settings_unit ON service_unit_settings (unit_id);
COMMENT ON TABLE service_unit_settings IS 'Xizmatning birlik (va ostidagilar) uchun narxi / yoqilganligi. Eng yaqin daraja ustun.';

CREATE TABLE template_assets (
  id            uuid          PRIMARY KEY DEFAULT uuidv7(),
  owner_unit_id uuid          NOT NULL REFERENCES org_units(id),
  kind          varchar(12)   NOT NULL,
  name          varchar(200)  NOT NULL,
  file_id       uuid          NOT NULL REFERENCES stored_files(id),
  width         numeric(8,2)  NOT NULL CHECK (width > 0),
  height        numeric(8,2)  NOT NULL CHECK (height > 0),
  employee_id   uuid          REFERENCES employees(id),      -- imzo kimniki
  created_by    uuid,
  created_at    timestamptz   NOT NULL DEFAULT now(),
  updated_at    timestamptz   NOT NULL DEFAULT now(),
  deleted_at    timestamptz,
  deleted_by    uuid,
  CONSTRAINT ck_template_assets_kind CHECK (kind IN ('logo', 'stamp', 'signature', 'image'))
);
CREATE INDEX ix_template_assets_owner ON template_assets (owner_unit_id, kind);
CREATE INDEX ix_template_assets_employee ON template_assets (employee_id);
COMMENT ON TABLE template_assets IS 'Shablon rasmlari: logo, muhr, imzo, boshqa rasm';

ALTER TABLE employees ADD CONSTRAINT fk_employees_signature_asset
  FOREIGN KEY (signature_asset_id) REFERENCES template_assets(id);

-- ---------------------------------------------------------------------------
-- resolution helpers
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION catalog_visible(owner_unit uuid, unit uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT unit_in_scope(unit, owner_unit)
$$;
COMMENT ON FUNCTION catalog_visible(uuid, uuid) IS 'Katalog yozuvi (egasi owner_unit) unit uchun ko''rinadimi';

CREATE OR REPLACE FUNCTION service_price_for(service uuid, unit uuid) RETURNS bigint
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(
    (SELECT s.price
     FROM service_unit_settings s
     JOIN org_unit_closure c ON c.ancestor_id = s.unit_id
     WHERE s.service_type_id = service AND c.descendant_id = unit AND s.price IS NOT NULL
     ORDER BY c.depth LIMIT 1),
    (SELECT price FROM service_types WHERE id = service))
$$;
COMMENT ON FUNCTION service_price_for(uuid, uuid) IS 'Xizmatning shu birlikdagi amaldagi narxi (eng yaqin o''zgartirish yoki asosiy narx)';

CREATE OR REPLACE FUNCTION service_enabled_for(service uuid, unit uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(
    (SELECT s.is_enabled
     FROM service_unit_settings s
     JOIN org_unit_closure c ON c.ancestor_id = s.unit_id
     WHERE s.service_type_id = service AND c.descendant_id = unit
     ORDER BY c.depth LIMIT 1),
    true)
    AND COALESCE((SELECT is_active AND deleted_at IS NULL FROM service_types WHERE id = service), false)
    AND catalog_visible((SELECT owner_unit_id FROM service_types WHERE id = service), unit)
$$;
COMMENT ON FUNCTION service_enabled_for(uuid, uuid) IS 'Xizmat shu birlikda ko''rsatiladimi (409 not_in_branch qoidasi)';

SELECT attach_updated_at_triggers();
