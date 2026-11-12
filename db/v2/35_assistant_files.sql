-- ============================================================================
-- TemoDB v2 · 35 · files the assistant produced
--   assistant_files  a spreadsheet / CSV / JSON the model built on request (export_data / export_table) — the
--                    employee downloads it from the chat; owner-only, kept for a few days, then purged
-- Idempotent: re-running changes nothing.
-- ============================================================================

CREATE TABLE IF NOT EXISTS assistant_files (
  id               uuid         PRIMARY KEY DEFAULT uuidv7(),
  thread_id        uuid         NOT NULL REFERENCES assistant_threads(id) ON DELETE CASCADE,
  employee_id      uuid         NOT NULL REFERENCES employees(id),
  tool_call_id     varchar(80),                                    -- the tool call that produced it
  name             varchar(200) NOT NULL,
  content_type     varchar(120) NOT NULL,
  size             integer      NOT NULL,
  rows             integer,
  data             bytea        NOT NULL,
  meta             jsonb        NOT NULL DEFAULT '{}'::jsonb,       -- dataset, columns, filters, sort, format
  expires_at       timestamptz  NOT NULL,
  created_at       timestamptz  NOT NULL DEFAULT now(),
  updated_at       timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_assistant_files_thread  ON assistant_files (thread_id, created_at);
CREATE INDEX IF NOT EXISTS ix_assistant_files_expires ON assistant_files (expires_at);
COMMENT ON TABLE assistant_files IS 'Yordamchi yasagan fayllar (Excel / CSV / JSON) — faqat egasi yuklab oladi, muddati o‘tgach o‘chiriladi';
