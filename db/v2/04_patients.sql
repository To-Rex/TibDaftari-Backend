-- ============================================================================
-- TemoDB v2 · 04 · patients — ONE registry per country
--
-- Today a patient is duplicated per company. Without companies the natural
-- identity scope is the country: a person seen in Bog'ot and later in Urganch
-- is the same patient. Lists stay scoped by the facility that registered them
-- (registered_unit_id) and by the units of their orders; identity is national.
-- Identity rules (same as production, per country instead of per company):
--   passport unique · PINFL unique · otherwise phone unique
-- ============================================================================

CREATE TABLE patients (
  id                     uuid         PRIMARY KEY DEFAULT uuidv7(),
  country_id             uuid         NOT NULL REFERENCES countries(id),
  registered_unit_id     uuid         REFERENCES org_units(id),   -- ro'yxatga olgan muassasa
  full_name              varchar(200) NOT NULL,
  phone                  varchar(20)  NOT NULL,                   -- 998XXXXXXXXX
  phone_extra            varchar(20),
  gender                 varchar(6),
  birth_date             date,
  passport_number        varchar(20),
  pinfl                  varchar(14),
  region_id              uuid         REFERENCES regions(id),     -- yashash manzili
  district_id            uuid         REFERENCES districts(id),
  street                 varchar(300),
  workplace              varchar(200),
  discount_percent       smallint     NOT NULL DEFAULT 0,
  contract_number        varchar(60),
  note                   text,
  tags                   text[]       NOT NULL DEFAULT '{}',
  -- denormalised stats (orders service maintains them)
  stats_orders           integer      NOT NULL DEFAULT 0,
  stats_last_visit_at    timestamptz,
  stats_total_spent      bigint       NOT NULL DEFAULT 0,
  -- portal / telegram
  portal_linked          boolean      NOT NULL DEFAULT false,
  telegram_chat_id       varchar(32),
  portal_last_login_at   timestamptz,
  portal_login           varchar(40),
  portal_password_hash   text,
  portal_password_set_at timestamptz,
  portal_failed_logins   smallint     NOT NULL DEFAULT 0,
  portal_locked_until    timestamptz,
  created_by             uuid,
  created_at             timestamptz  NOT NULL DEFAULT now(),
  updated_at             timestamptz  NOT NULL DEFAULT now(),
  deleted_at             timestamptz,
  deleted_by             uuid,
  CONSTRAINT ck_patients_gender   CHECK (gender IS NULL OR gender IN ('male', 'female')),
  CONSTRAINT ck_patients_discount CHECK (discount_percent BETWEEN 0 AND 100),
  CONSTRAINT ck_patients_pinfl    CHECK (pinfl IS NULL OR pinfl ~ '^[0-9]{14}$'),
  CONSTRAINT ck_patients_stats    CHECK (stats_orders >= 0 AND stats_total_spent >= 0)
);
-- identity (per country)
CREATE UNIQUE INDEX uq_patients_country_passport_alive ON patients (country_id, passport_number)
  WHERE deleted_at IS NULL AND passport_number IS NOT NULL;
CREATE UNIQUE INDEX uq_patients_country_pinfl_alive ON patients (country_id, pinfl)
  WHERE deleted_at IS NULL AND pinfl IS NOT NULL;
CREATE UNIQUE INDEX uq_patients_country_phone_noid_alive ON patients (country_id, phone)
  WHERE deleted_at IS NULL AND passport_number IS NULL AND pinfl IS NULL;
CREATE UNIQUE INDEX uq_patients_portal_login_alive ON patients (lower(portal_login))
  WHERE deleted_at IS NULL AND portal_login IS NOT NULL;
-- lookups
CREATE INDEX ix_patients_country_phone    ON patients (country_id, phone);
CREATE INDEX ix_patients_country_created  ON patients (country_id, created_at);
CREATE INDEX ix_patients_registered_unit  ON patients (registered_unit_id, created_at);
CREATE INDEX ix_patients_home_district    ON patients (district_id);
CREATE INDEX ix_patients_telegram_chat    ON patients (telegram_chat_id);
CREATE INDEX ix_patients_fold_name_trgm   ON patients USING gin (fold_text(full_name) gin_trgm_ops);
CREATE INDEX ix_patients_phone_trgm       ON patients USING gin (phone gin_trgm_ops);
CREATE INDEX ix_patients_passport_trgm    ON patients USING gin (fold_text(COALESCE(passport_number, '')) gin_trgm_ops);

CREATE TRIGGER trg_patients_registered_unit_level BEFORE INSERT OR UPDATE OF registered_unit_id ON patients
  FOR EACH ROW EXECUTE FUNCTION assert_unit_level('registered_unit_id', 'facility');

COMMENT ON TABLE  patients                    IS 'Bemorlar — davlat bo''yicha yagona reyestr (kompaniya bo''yicha emas)';
COMMENT ON COLUMN patients.registered_unit_id IS 'Bemorni birinchi ro''yxatga olgan muassasa (ro''yxatlar shu bo''yicha filtrlanadi)';
COMMENT ON COLUMN patients.portal_login       IS 'Qabulxona bergan portal logini (telefon + SMS koddan tashqari yo''l); tizim bo''yicha yagona';

SELECT attach_updated_at_triggers();
