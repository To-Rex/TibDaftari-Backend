-- ============================================================================
-- TemoDB v2 · 08 · audit log — append-only, range-partitioned by month
-- unit_id + country_id are denormalised so a viloyat / davlat audit view is a
-- simple indexed filter.
-- ============================================================================

CREATE SEQUENCE audit_log_id_seq AS bigint START WITH 1 INCREMENT BY 1 CACHE 50;

CREATE TABLE audit_log (
  id         bigint       NOT NULL DEFAULT nextval('audit_log_id_seq'),
  created_at timestamptz  NOT NULL DEFAULT now(),
  country_id uuid,
  unit_id    uuid,
  actor_type varchar(10)  NOT NULL,
  actor_id   uuid,
  action     varchar(40)  NOT NULL,                 -- create | update | delete | login | approve | …
  entity     varchar(40)  NOT NULL,
  entity_id  varchar(64),
  before     jsonb,
  after      jsonb,
  ip         varchar(64),
  request_id varchar(40),
  CONSTRAINT pk_audit_log PRIMARY KEY (id, created_at),
  CONSTRAINT ck_audit_log_actor CHECK (actor_type IN ('staff', 'patient', 'system'))
) PARTITION BY RANGE (created_at);
ALTER SEQUENCE audit_log_id_seq OWNED BY audit_log.id;
CREATE INDEX ix_audit_log_unit_created    ON audit_log (unit_id, created_at);
CREATE INDEX ix_audit_log_country_created ON audit_log (country_id, created_at);
CREATE INDEX ix_audit_log_entity          ON audit_log (entity, entity_id);
CREATE INDEX ix_audit_log_actor           ON audit_log (actor_id, created_at);
COMMENT ON TABLE audit_log IS 'O''zgarishlar jurnali (faqat qo''shiladi). Oylik bo''laklar: audit_log_YYYYMM + audit_log_default.';

-- one partition for a given month (idempotent)
CREATE OR REPLACE FUNCTION ensure_audit_partition_for(month_start date) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE m date := date_trunc('month', month_start)::date; part text;
BEGIN
  part := format('audit_log_%s', to_char(m, 'YYYYMM'));
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = part) THEN
    EXECUTE format('CREATE TABLE %I PARTITION OF audit_log FOR VALUES FROM (%L) TO (%L)',
                   part, m, (m + interval '1 month')::date);
  END IF;
  RETURN part;
END $$;

-- same signature as production: current month -1 … +months_ahead, plus the default partition
CREATE OR REPLACE FUNCTION ensure_audit_partitions(months_ahead integer DEFAULT 2) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE i integer;
BEGIN
  FOR i IN -1..months_ahead LOOP
    PERFORM ensure_audit_partition_for((date_trunc('month', now()) + (i || ' month')::interval)::date);
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'audit_log_default') THEN
    EXECUTE 'CREATE TABLE audit_log_default PARTITION OF audit_log DEFAULT';
  END IF;
END $$;
COMMENT ON FUNCTION ensure_audit_partitions(integer) IS 'Oylik bo''laklarni oldindan yaratadi (backend ishga tushganda chaqiradi)';

-- partitions covering the production history (2026-07 …) through next spring
DO $$
DECLARE m date := date '2026-07-01';
BEGIN
  WHILE m <= date '2027-06-01' LOOP
    PERFORM ensure_audit_partition_for(m);
    m := (m + interval '1 month')::date;
  END LOOP;
END $$;
SELECT ensure_audit_partitions(3);
