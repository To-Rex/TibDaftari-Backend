-- ============================================================================
-- TemoDB v2 · 01 · geo reference  (Davlat → Viloyat → Tuman/Shahar)
-- Pure reference data (ids identical to production so migration is a copy).
-- Countries also carry the per-country operating settings that used to be
-- hard-coded (currency, locale, phone format, timezone).
-- ============================================================================

CREATE TABLE countries (
  id             uuid        PRIMARY KEY DEFAULT uuidv7(),
  code           varchar(2)  NOT NULL UNIQUE,                 -- ISO 3166-1 alpha-2
  name           varchar(120) NOT NULL,                       -- o'zbekcha
  name_ru        varchar(120),
  name_en        varchar(120),
  phone_code     varchar(8),                                  -- '+998'
  sort_order     integer     NOT NULL DEFAULT 0,
  -- per-country operating settings (3-bosqich uchun tayyor)
  currency_code  char(3),                                     -- ISO 4217: 'UZS'
  default_locale varchar(2)  NOT NULL DEFAULT 'uz',
  locales        text[]      NOT NULL DEFAULT '{uz,ru,en}',
  timezone       varchar(64) NOT NULL DEFAULT 'Asia/Tashkent',
  phone_mask     varchar(40),                                 -- '+998 ## ###-##-##'
  is_enabled     boolean     NOT NULL DEFAULT false,          -- tizim shu davlatda ishga tushirilganmi
  CONSTRAINT ck_countries_code_upper CHECK (code = upper(code)),
  CONSTRAINT ck_countries_locale     CHECK (default_locale = ANY (locales))
);
COMMENT ON TABLE  countries            IS 'Davlatlar (ISO 3166-1) + davlat darajasidagi sozlamalar';
COMMENT ON COLUMN countries.is_enabled IS 'true = bu davlatda tizim ishlaydi (org_units daraxti ochilishi mumkin)';
COMMENT ON COLUMN countries.locales    IS 'Interfeys tillari; default_locale shu ro''yxatda bo''lishi shart';

CREATE TABLE regions (
  id         uuid         PRIMARY KEY DEFAULT uuidv7(),
  country_id uuid         NOT NULL REFERENCES countries(id),
  name       varchar(120) NOT NULL,
  code       varchar(20),
  sort_order integer      NOT NULL DEFAULT 0
);
CREATE INDEX ix_regions_country ON regions (country_id, sort_order);
CREATE UNIQUE INDEX uq_regions_country_code ON regions (country_id, lower(code)) WHERE code IS NOT NULL;
COMMENT ON TABLE regions IS 'Viloyatlar / respublika ahamiyatidagi shaharlar (davlatning 1-darajali bo''linmasi)';

CREATE TABLE districts (
  id         uuid         PRIMARY KEY DEFAULT uuidv7(),
  region_id  uuid         NOT NULL REFERENCES regions(id),
  name       varchar(120) NOT NULL,
  code       varchar(20),
  sort_order integer      NOT NULL DEFAULT 0,
  kind       varchar(10)  NOT NULL DEFAULT 'district',
  CONSTRAINT ck_districts_kind CHECK (kind IN ('district', 'city'))
);
CREATE INDEX ix_districts_region ON districts (region_id, sort_order);
CREATE UNIQUE INDEX uq_districts_region_code ON districts (region_id, lower(code)) WHERE code IS NOT NULL;
COMMENT ON TABLE  districts      IS 'Tumanlar va shaharlar (viloyatning bo''linmasi)';
COMMENT ON COLUMN districts.kind IS 'district = tuman, city = shahar';
