-- ============================================================================
-- TemoDB v2 · 07 · messaging: provider accounts, SMS texts, outbox, in-app, Telegram
--
-- SMS / Telegram credentials used to sit on the company (and optionally the
-- branch). Now they are messaging_accounts attached to ANY unit and inherited
-- downwards: one national Xabarchi key, a viloyat's own key, or a facility's.
-- resolve_messaging_account(unit, channel) picks the nearest active one.
-- SMS texts (sms_templates) inherit the same way (replaces settings.smsTemplates).
-- ============================================================================

CREATE TABLE messaging_accounts (
  id               uuid         PRIMARY KEY DEFAULT uuidv7(),
  unit_id          uuid         NOT NULL REFERENCES org_units(id),
  channel          varchar(10)  NOT NULL,                          -- sms | telegram
  provider         varchar(20)  NOT NULL,                          -- sms: xabarchi | none ; telegram: telegram
  api_key_enc      text,                                           -- Fernet bilan shifrlangan kalit / bot token
  api_key_masked   varchar(80),
  default_priority varchar(20)  NOT NULL DEFAULT 'transactional',
  sender_note      varchar(200),
  bot_username     varchar(80),                                    -- telegram
  is_active        boolean      NOT NULL DEFAULT true,
  created_by       uuid,
  created_at       timestamptz  NOT NULL DEFAULT now(),
  updated_at       timestamptz  NOT NULL DEFAULT now(),
  deleted_at       timestamptz,
  deleted_by       uuid,
  CONSTRAINT ck_messaging_accounts_channel  CHECK (channel IN ('sms', 'telegram')),
  CONSTRAINT ck_messaging_accounts_provider CHECK (
       (channel = 'sms'      AND provider IN ('xabarchi', 'none'))
    OR (channel = 'telegram' AND provider = 'telegram')),
  CONSTRAINT ck_messaging_accounts_key CHECK (provider = 'none' OR api_key_enc IS NOT NULL)
);
CREATE UNIQUE INDEX uq_messaging_accounts_unit_channel_alive ON messaging_accounts (unit_id, channel) WHERE deleted_at IS NULL;
COMMENT ON TABLE  messaging_accounts          IS 'SMS (Xabarchi) va Telegram bot hisoblari — birlikka biriktiriladi, pastga meros bo''ladi';
COMMENT ON COLUMN messaging_accounts.provider IS 'none = bu birlik (va ostidagilar) SMS yubormaydi — yuqoridagi kalit ham ishlatilmaydi';

CREATE OR REPLACE FUNCTION resolve_messaging_account(unit uuid, chan text) RETURNS messaging_accounts
LANGUAGE sql STABLE AS $$
  SELECT a.*
  FROM messaging_accounts a
  JOIN org_unit_closure c ON c.ancestor_id = a.unit_id
  WHERE c.descendant_id = unit AND a.channel = chan AND a.deleted_at IS NULL AND a.is_active
  ORDER BY c.depth LIMIT 1
$$;
COMMENT ON FUNCTION resolve_messaging_account(uuid, text) IS 'Birlik uchun amaldagi SMS/Telegram hisobi (eng yaqin faol hisob)';

CREATE TABLE sms_templates (
  id         uuid         PRIMARY KEY DEFAULT uuidv7(),
  unit_id    uuid         NOT NULL REFERENCES org_units(id),
  key        varchar(40)  NOT NULL,                                -- payment_receipt | result_ready | reminder | otp | document_code | …
  lang       varchar(2)   NOT NULL DEFAULT 'uz',
  body       text         NOT NULL,
  is_active  boolean      NOT NULL DEFAULT true,
  created_by uuid,
  created_at timestamptz  NOT NULL DEFAULT now(),
  updated_at timestamptz  NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  deleted_by uuid,
  CONSTRAINT ck_sms_templates_key CHECK (key ~ '^[a-z][a-z0-9_]{1,39}$')
);
CREATE UNIQUE INDEX uq_sms_templates_unit_key_lang_alive ON sms_templates (unit_id, key, lang) WHERE deleted_at IS NULL;
COMMENT ON TABLE sms_templates IS 'SMS matnlari ({{bemor}}, {{chek}}, {{havola}} …). Yaqinroq daraja ustun; yo''q bo''lsa koddagi standart matn.';

CREATE OR REPLACE FUNCTION resolve_sms_template(unit uuid, tkey text, tlang text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT t.body
  FROM sms_templates t
  JOIN org_unit_closure c ON c.ancestor_id = t.unit_id
  WHERE c.descendant_id = unit AND t.key = tkey AND t.lang = tlang AND t.deleted_at IS NULL AND t.is_active
  ORDER BY c.depth LIMIT 1
$$;

CREATE TABLE outbox_messages (
  id                  uuid         PRIMARY KEY DEFAULT uuidv7(),
  unit_id             uuid         REFERENCES org_units(id),      -- yuborgan muassasa
  account_id          uuid         REFERENCES messaging_accounts(id),
  patient_id          uuid         REFERENCES patients(id),
  order_id            uuid         REFERENCES orders(id),
  document_id         uuid         REFERENCES result_documents(id),
  channel             varchar(10)  NOT NULL,                      -- sms | telegram | portal
  kind                varchar(20)  NOT NULL,                      -- payment_receipt | result_ready | reminder | broadcast | otp | …
  "to"                varchar(64)  NOT NULL,                      -- telefon yoki chat id
  text                text         NOT NULL,
  status              varchar(10)  NOT NULL DEFAULT 'queued',
  scheduled_at        timestamptz,
  sent_at             timestamptz,
  attempts            smallint     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at     timestamptz,
  leased_until        timestamptz,
  provider_message_id varchar(64),
  error               text,
  payload             jsonb        NOT NULL DEFAULT '{}'::jsonb,
  created_by          uuid,
  created_at          timestamptz  NOT NULL DEFAULT now(),
  updated_at          timestamptz  NOT NULL DEFAULT now(),
  deleted_at          timestamptz,
  deleted_by          uuid,
  CONSTRAINT ck_outbox_channel CHECK (channel IN ('sms', 'telegram', 'portal')),
  CONSTRAINT ck_outbox_status  CHECK (status IN ('scheduled', 'queued', 'sending', 'sent', 'delivered', 'failed', 'cancelled'))
);
CREATE INDEX ix_outbox_unit_created        ON outbox_messages (unit_id, created_at);
CREATE INDEX ix_outbox_unit_status_created ON outbox_messages (unit_id, status, created_at);
CREATE INDEX ix_outbox_patient             ON outbox_messages (patient_id);
CREATE INDEX ix_outbox_order               ON outbox_messages (order_id);
CREATE INDEX ix_outbox_document            ON outbox_messages (document_id);
CREATE INDEX ix_outbox_sent                ON outbox_messages (sent_at) WHERE sent_at IS NOT NULL;
CREATE INDEX ix_outbox_queue               ON outbox_messages (next_attempt_at)
  WHERE status IN ('queued', 'scheduled') AND deleted_at IS NULL;
CREATE INDEX ix_outbox_fold_text_trgm      ON outbox_messages USING gin (fold_text(text) gin_trgm_ops);
COMMENT ON TABLE outbox_messages IS 'Yuboriladigan xabarlar navbati (SMS / Telegram / portal): rejalashtirish, qayta urinish, provayder javobi';

CREATE TABLE notifications (
  id          uuid         PRIMARY KEY DEFAULT uuidv7(),
  unit_id     uuid         REFERENCES org_units(id),              -- qamrov: shu birlik va ostidagilar; NULL = hamma
  employee_id uuid         REFERENCES employees(id),              -- NULL = qamrovdagi hamma xodim
  title       varchar(200) NOT NULL,
  body        text         NOT NULL,
  kind        varchar(10)  NOT NULL DEFAULT 'info',
  link        varchar(300),
  read_by     uuid[]       NOT NULL DEFAULT '{}',
  created_by  uuid,
  created_at  timestamptz  NOT NULL DEFAULT now(),
  updated_at  timestamptz  NOT NULL DEFAULT now(),
  deleted_at  timestamptz,
  deleted_by  uuid,
  CONSTRAINT ck_notifications_kind CHECK (kind IN ('info', 'success', 'warning', 'error'))
);
CREATE INDEX ix_notifications_unit_created ON notifications (unit_id, created_at);
CREATE INDEX ix_notifications_employee     ON notifications (employee_id, created_at);
COMMENT ON TABLE notifications IS 'Ilova ichidagi bildirishnomalar';

CREATE TABLE telegram_links (
  id          uuid         PRIMARY KEY DEFAULT uuidv7(),
  account_id  uuid         REFERENCES messaging_accounts(id),     -- qaysi bot
  patient_id  uuid         NOT NULL REFERENCES patients(id),
  chat_id     varchar(32)  NOT NULL,
  lang        varchar(4)   NOT NULL DEFAULT 'uz',
  linked_at   timestamptz  NOT NULL DEFAULT now(),
  unlinked_at timestamptz
);
CREATE INDEX ix_telegram_links_chat    ON telegram_links (chat_id);
CREATE INDEX ix_telegram_links_patient ON telegram_links (patient_id);
CREATE UNIQUE INDEX uq_telegram_links_active ON telegram_links (account_id, chat_id, patient_id) WHERE unlinked_at IS NULL;
COMMENT ON TABLE telegram_links IS 'Bemor ↔ Telegram chat bog''lanish tarixi (uzish = unlinked_at)';

CREATE TABLE telegram_chat_prefs (
  account_id uuid        NOT NULL REFERENCES messaging_accounts(id),
  chat_id    varchar(32) NOT NULL,
  lang       varchar(4)  NOT NULL DEFAULT 'uz',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, chat_id)
);
COMMENT ON TABLE telegram_chat_prefs IS 'Chat tili (bog''lanmagan bo''lsa ham saqlanadi)';

SELECT attach_updated_at_triggers();
