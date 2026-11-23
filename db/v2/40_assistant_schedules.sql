-- ============================================================================
-- TemoDB v2 · 40 · the assistant's scheduled tasks («Rejali topshiriqlar»)
--   assistant_schedules  a task one employee left for the assistant to repeat: what to do (prompt), when (daily /
--                        on week days / on a day of the month / once, at a local time), in which unit. Every run
--                        happens with the OWNER's rights as they are at that moment, with read-only tools, and
--                        lands in the task's own conversation.
--   assistant_threads    + schedule_id (the conversation belongs to a task), + unread (a run left an answer the
--                        owner has not opened yet)
-- Idempotent: re-running changes nothing.
-- ============================================================================

CREATE TABLE IF NOT EXISTS assistant_schedules (
  id               uuid         PRIMARY KEY DEFAULT uuidv7(),
  employee_id      uuid         NOT NULL REFERENCES employees(id),
  unit_id          uuid         NOT NULL REFERENCES org_units(id),           -- the working unit of every run
  thread_id        uuid         REFERENCES assistant_threads(id) ON DELETE SET NULL,
  title            varchar(200) NOT NULL,
  prompt           text         NOT NULL,                                    -- what the assistant is asked at every run
  kind             varchar(10)  NOT NULL,                                    -- daily | weekly | monthly | once
  at_time          varchar(5)   NOT NULL,                                    -- HH:MM, in `timezone`
  weekdays         smallint[]   NOT NULL DEFAULT '{}',                       -- weekly: 1 = dushanba … 7 = yakshanba
  month_day        smallint,                                                 -- monthly: 1…31 (a shorter month → its last day)
  run_date         date,                                                     -- once
  timezone         varchar(64)  NOT NULL DEFAULT 'Asia/Tashkent',
  is_active        boolean      NOT NULL DEFAULT true,
  paused_reason    text,                                                     -- why the SYSTEM switched it off (errors, lost rights)
  next_run_at      timestamptz,                                              -- NULL = nothing ahead (paused, or a `once` that ran)
  last_run_at      timestamptz,
  last_status      varchar(10),                                              -- ok | failed
  last_error       text,
  run_count        integer      NOT NULL DEFAULT 0,
  fail_count       integer      NOT NULL DEFAULT 0,                          -- failures in a row
  created_at       timestamptz  NOT NULL DEFAULT now(),
  updated_at       timestamptz  NOT NULL DEFAULT now(),
  deleted_at       timestamptz,
  deleted_by       uuid,
  CONSTRAINT ck_assistant_schedules_kind CHECK (kind IN ('daily', 'weekly', 'monthly', 'once')),
  CONSTRAINT ck_assistant_schedules_time CHECK (at_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
  CONSTRAINT ck_assistant_schedules_status CHECK (last_status IS NULL OR last_status IN ('ok', 'failed'))
);
CREATE INDEX IF NOT EXISTS ix_assistant_schedules_due      ON assistant_schedules (next_run_at) WHERE is_active AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_assistant_schedules_employee ON assistant_schedules (employee_id) WHERE deleted_at IS NULL;
COMMENT ON TABLE  assistant_schedules IS 'Yordamchining rejali topshiriqlari: egasi nomidan, uning shu paytdagi huquqlari bilan, faqat o‘qish vositalari orqali bajariladi';
COMMENT ON COLUMN assistant_schedules.paused_reason IS 'Tizim o‘zi to‘xtatgan bo‘lsa — sababi (ketma-ket xatolar, huquq yoki hudud yo‘qolgan)';

ALTER TABLE assistant_threads ADD COLUMN IF NOT EXISTS schedule_id uuid;
ALTER TABLE assistant_threads ADD COLUMN IF NOT EXISTS unread boolean NOT NULL DEFAULT false;
COMMENT ON COLUMN assistant_threads.schedule_id IS 'Suhbat rejali topshiriqniki (natijalari shu yerga tushadi)';
COMMENT ON COLUMN assistant_threads.unread IS 'Rejali topshiriq javob qoldirgan, egasi hali ochmagan';
