-- ============================================================================
-- TemoDB v2 · 06 · orders (cheques) → items (services) → payments; documents
-- unit_id on every row = the FACILITY that did the work. Territory statistics
-- (viloyat / tuman) are folded through org_unit_closure from this column.
-- ============================================================================

CREATE TABLE orders (
  id                     uuid          PRIMARY KEY DEFAULT uuidv7(),
  unit_id                uuid          NOT NULL REFERENCES org_units(id),
  number                 varchar(24)   NOT NULL,                  -- UR-000123 (next_order_number)
  patient_id             uuid          NOT NULL REFERENCES patients(id),
  patient_name           varchar(200)  NOT NULL,                  -- snapshot
  patient_phone          varchar(20)   NOT NULL,
  created_by_employee_id uuid          NOT NULL REFERENCES employees(id),
  status                 varchar(12)   NOT NULL DEFAULT 'open',
  payment                varchar(10)   NOT NULL DEFAULT 'unpaid',
  subtotal               money_uzs     NOT NULL DEFAULT 0,
  discount_percent       percent_0_100 NOT NULL DEFAULT 0,
  discount_amount        money_uzs     NOT NULL DEFAULT 0,
  total                  money_uzs     NOT NULL DEFAULT 0,
  paid_amount            money_uzs     NOT NULL DEFAULT 0,
  item_count             integer       NOT NULL DEFAULT 0 CHECK (item_count >= 0),
  progress               jsonb         NOT NULL DEFAULT '{"pending": 0, "entered": 0, "submitted": 0, "approved": 0, "rejected": 0, "cancelled": 0}'::jsonb,
  note                   text,
  first_paid_at          timestamptz,                             -- 1 kunlik tahrir oynasi shu vaqtdan hisoblanadi
  cancelled_at           timestamptz,
  cancel_reason          text,
  completed_at           timestamptz,
  created_by             uuid,
  created_at             timestamptz   NOT NULL DEFAULT now(),
  updated_at             timestamptz   NOT NULL DEFAULT now(),
  deleted_at             timestamptz,
  deleted_by             uuid,
  CONSTRAINT ck_orders_status   CHECK (status IN ('draft', 'open', 'in_progress', 'completed', 'cancelled')),
  CONSTRAINT ck_orders_payment  CHECK (payment IN ('unpaid', 'partial', 'paid', 'refunded')),
  CONSTRAINT ck_orders_progress CHECK (jsonb_typeof(progress) = 'object'),
  CONSTRAINT uq_orders_unit_number UNIQUE (unit_id, number)
);
CREATE INDEX ix_orders_unit_created      ON orders (unit_id, created_at);
CREATE INDEX ix_orders_unit_status       ON orders (unit_id, status);
CREATE INDEX ix_orders_created           ON orders (created_at);
CREATE INDEX ix_orders_patient_created   ON orders (patient_id, created_at);
CREATE INDEX ix_orders_employee          ON orders (created_by_employee_id);
CREATE INDEX ix_orders_number_lower      ON orders (lower(number) text_pattern_ops);
CREATE INDEX ix_orders_fold_patient_trgm ON orders USING gin (fold_text(patient_name) gin_trgm_ops);
CREATE TRIGGER trg_orders_unit_level BEFORE INSERT OR UPDATE OF unit_id ON orders
  FOR EACH ROW EXECUTE FUNCTION assert_unit_level('unit_id', 'facility');
COMMENT ON TABLE  orders               IS 'Cheklar. unit_id = ishni bajargan muassasa; hudud statistikasi shu ustundan yig''iladi.';
COMMENT ON COLUMN orders.progress      IS '{pending, entered, submitted, approved, rejected, cancelled}: tahlillar soni holat bo''yicha';
COMMENT ON COLUMN orders.first_paid_at IS 'Birinchi to''lov vaqti — qabulxona to''lovni shu vaqtdan 24 soat ichida tahrirlay oladi';

CREATE TABLE order_items (
  id               uuid          PRIMARY KEY DEFAULT uuidv7(),
  order_id         uuid          NOT NULL REFERENCES orders(id),
  unit_id          uuid          NOT NULL REFERENCES org_units(id),
  service_type_id  uuid          NOT NULL REFERENCES service_types(id),
  service_name     varchar(300)  NOT NULL,                       -- snapshot
  category_id      uuid          NOT NULL REFERENCES categories(id),
  category_name    varchar(200)  NOT NULL,
  price            money_uzs     NOT NULL DEFAULT 0,
  final_price      money_uzs     NOT NULL DEFAULT 0,
  status           varchar(12)   NOT NULL DEFAULT 'pending',
  schema_id        uuid          REFERENCES attribute_schemas(id),
  schema_version   integer,
  "values"         jsonb         NOT NULL DEFAULT '{}'::jsonb,
  technician_id    uuid          REFERENCES employees(id),
  technician_name  varchar(200),
  entered_at       timestamptz,
  submitted_at     timestamptz,
  doctor_id        uuid          REFERENCES employees(id),
  doctor_name      varchar(200),
  approved_at      timestamptz,
  reject_reason    text,
  document_id      uuid,                                          -- FK → result_documents (quyida)
  lab_note         text,
  history          jsonb         NOT NULL DEFAULT '[]'::jsonb,   -- [{type, at, byId, byName, reason?, documentId?}]
  created_by       uuid,
  created_at       timestamptz   NOT NULL DEFAULT now(),
  updated_at       timestamptz   NOT NULL DEFAULT now(),
  deleted_at       timestamptz,
  deleted_by       uuid,
  CONSTRAINT ck_order_items_status  CHECK (status IN ('pending', 'entered', 'submitted', 'approved', 'rejected', 'cancelled')),
  CONSTRAINT ck_order_items_values  CHECK (jsonb_typeof("values") = 'object'),
  CONSTRAINT ck_order_items_history CHECK (jsonb_typeof(history) = 'array')
);
CREATE INDEX ix_order_items_order                ON order_items (order_id);
CREATE INDEX ix_order_items_unit_created         ON order_items (unit_id, created_at);
CREATE INDEX ix_order_items_unit_status_created  ON order_items (unit_id, status, created_at);
CREATE INDEX ix_order_items_category_status      ON order_items (category_id, status);
CREATE INDEX ix_order_items_service_created      ON order_items (service_type_id, created_at);
CREATE INDEX ix_order_items_technician           ON order_items (technician_id);
CREATE INDEX ix_order_items_doctor               ON order_items (doctor_id);
CREATE INDEX ix_order_items_fold_service_trgm    ON order_items USING gin (fold_text(service_name) gin_trgm_ops);
CREATE TRIGGER trg_order_items_unit_level BEFORE INSERT OR UPDATE OF unit_id ON order_items
  FOR EACH ROW EXECUTE FUNCTION assert_unit_level('unit_id', 'facility');
COMMENT ON TABLE order_items IS 'Chekdagi tahlillar: natija qiymatlari (values), laborant/vrach, holat tarixi';

CREATE TABLE payments (
  id            uuid         PRIMARY KEY DEFAULT uuidv7(),
  order_id      uuid         NOT NULL REFERENCES orders(id),
  unit_id       uuid         NOT NULL REFERENCES org_units(id),
  amount        bigint       NOT NULL CHECK (amount > 0),
  method        varchar(12)  NOT NULL,
  employee_id   uuid         NOT NULL REFERENCES employees(id),
  note          text,
  refunded_at   timestamptz,
  refund_reason text,
  corrections   jsonb        NOT NULL DEFAULT '[]'::jsonb,       -- [{at, byId, byName, fromMethod, fromAmount, reason, splitFrom?}]
  created_by    uuid,
  created_at    timestamptz  NOT NULL DEFAULT now(),
  updated_at    timestamptz  NOT NULL DEFAULT now(),
  deleted_at    timestamptz,
  deleted_by    uuid,
  CONSTRAINT ck_payments_method      CHECK (method IN ('cash', 'card', 'transfer', 'insurance')),
  CONSTRAINT ck_payments_corrections CHECK (jsonb_typeof(corrections) = 'array')
);
CREATE INDEX ix_payments_order        ON payments (order_id);
CREATE INDEX ix_payments_unit_created ON payments (unit_id, created_at);
CREATE INDEX ix_payments_employee     ON payments (employee_id);
CREATE TRIGGER trg_payments_unit_level BEFORE INSERT OR UPDATE OF unit_id ON payments
  FOR EACH ROW EXECUTE FUNCTION assert_unit_level('unit_id', 'facility');
COMMENT ON TABLE  payments             IS 'To''lovlar (naqd / karta / o''tkazma / sug''urta). Qaytarish = refunded_at.';
COMMENT ON COLUMN payments.corrections IS 'To''lov usuli tuzatishlari tarixi (eng eskisi birinchi)';

-- orders.first_paid_at is set by the first payment
CREATE OR REPLACE FUNCTION payments_set_first_paid() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE orders SET first_paid_at = COALESCE(first_paid_at, NEW.created_at)
  WHERE id = NEW.order_id AND first_paid_at IS NULL;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_payments_first_paid AFTER INSERT ON payments
  FOR EACH ROW EXECUTE FUNCTION payments_set_first_paid();

CREATE TABLE result_documents (
  id                     uuid         PRIMARY KEY DEFAULT uuidv7(),
  order_id               uuid         NOT NULL REFERENCES orders(id),
  patient_id             uuid         NOT NULL REFERENCES patients(id),
  unit_id                uuid         NOT NULL REFERENCES org_units(id),
  order_item_id          uuid         REFERENCES order_items(id),
  order_item_ids         uuid[]       NOT NULL DEFAULT '{}',
  template_id            uuid         NOT NULL REFERENCES result_templates(id),
  template_version       integer      NOT NULL DEFAULT 1,
  title                  varchar(300) NOT NULL,
  status                 varchar(10)  NOT NULL DEFAULT 'final',
  pdf_file_id            uuid         REFERENCES stored_files(id),
  deliveries             jsonb        NOT NULL DEFAULT '[]'::jsonb,
  snapshot               jsonb        NOT NULL DEFAULT '{}'::jsonb,   -- muzlatilgan render ma'lumoti + shablon
  public_token           varchar(64),                                 -- /d/{token}  (SMS / Telegram havolasi)
  verify_token           varchar(64),                                 -- /v/{token}  (QR — faqat haqiqiylik)
  viewed_at              timestamptz,
  last_viewed_at         timestamptz,
  view_count             integer      NOT NULL DEFAULT 0 CHECK (view_count >= 0),
  printed_at             timestamptz,
  print_count            integer      NOT NULL DEFAULT 0 CHECK (print_count >= 0),
  public_view_count      integer      NOT NULL DEFAULT 0 CHECK (public_view_count >= 0),
  public_first_viewed_at timestamptz,
  public_last_viewed_at  timestamptz,
  created_by             uuid,
  created_at             timestamptz  NOT NULL DEFAULT now(),
  updated_at             timestamptz  NOT NULL DEFAULT now(),
  deleted_at             timestamptz,
  deleted_by             uuid,
  CONSTRAINT ck_result_documents_status     CHECK (status IN ('draft', 'final', 'revoked')),
  CONSTRAINT ck_result_documents_deliveries CHECK (jsonb_typeof(deliveries) = 'array'),
  CONSTRAINT ck_result_documents_snapshot   CHECK (jsonb_typeof(snapshot) = 'object')
);
CREATE UNIQUE INDEX uq_result_documents_public_token ON result_documents (public_token) WHERE public_token IS NOT NULL;
CREATE UNIQUE INDEX uq_result_documents_verify_token ON result_documents (verify_token) WHERE verify_token IS NOT NULL;
CREATE INDEX ix_result_documents_order        ON result_documents (order_id);
CREATE INDEX ix_result_documents_patient      ON result_documents (patient_id, created_at);
CREATE INDEX ix_result_documents_unit_created ON result_documents (unit_id, created_at);
CREATE INDEX ix_result_documents_item         ON result_documents (order_item_id);
CREATE INDEX ix_result_documents_items        ON result_documents USING gin (order_item_ids);
CREATE TRIGGER trg_result_documents_unit_level BEFORE INSERT OR UPDATE OF unit_id ON result_documents
  FOR EACH ROW EXECUTE FUNCTION assert_unit_level('unit_id', 'facility');
COMMENT ON TABLE  result_documents              IS 'Natija hujjatlari (PDF). snapshot bilan har doim qayta render qilinadi.';
COMMENT ON COLUMN result_documents.public_token IS '/d/{token} — bemorga yuboriladigan havola; birinchi ochish kodsiz (documentFreeViewDays)';
COMMENT ON COLUMN result_documents.verify_token IS '/v/{token} — QR kod; faqat haqiqiylikni tasdiqlaydi, PDF ochmaydi';

ALTER TABLE order_items ADD CONSTRAINT fk_order_items_document
  FOREIGN KEY (document_id) REFERENCES result_documents(id);

SELECT attach_updated_at_triggers();
