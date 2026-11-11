-- ============================================================================
-- TemoDB v2 · 33 · the AI assistant («Yordamchi»)
--   assistant_accounts  the model account a unit talks to (provider, model, encrypted key) — inherited downwards
--                       like messaging accounts: the nearest active one on the path is used
--   assistant_threads   one employee's conversations (the scope they were opened in)
--   assistant_messages  the turns of a thread: user / assistant (with the tool calls it asked for) / tool results
--   assistant_actions   a write the model proposed — runs only after the SAME employee confirms it in the UI
--   assistant.use       the permission every system role gets; what the assistant may DO is gated by the
--                       employee's other keys (every tool runs through the ordinary service functions)
-- Idempotent: re-running changes nothing.
-- ============================================================================

CREATE TABLE IF NOT EXISTS assistant_accounts (
  id               uuid         PRIMARY KEY DEFAULT uuidv7(),
  unit_id          uuid         NOT NULL REFERENCES org_units(id),
  provider         varchar(20)  NOT NULL,                          -- anthropic | openai | deepseek | deploy | custom | none
  base_url         varchar(300),                                   -- OpenAI-compatible gateways; empty = the provider default
  model            varchar(120),
  api_key_enc      text,                                           -- Fernet bilan shifrlangan kalit
  api_key_masked   varchar(80),
  settings         jsonb        NOT NULL DEFAULT '{}'::jsonb,       -- effort, maxTokens, temperature …
  is_active        boolean      NOT NULL DEFAULT true,
  created_by       uuid,
  created_at       timestamptz  NOT NULL DEFAULT now(),
  updated_at       timestamptz  NOT NULL DEFAULT now(),
  deleted_at       timestamptz,
  deleted_by       uuid,
  CONSTRAINT ck_assistant_accounts_provider CHECK (provider IN ('anthropic', 'openai', 'deepseek', 'deploy', 'custom', 'none')),
  CONSTRAINT ck_assistant_accounts_key CHECK (provider = 'none' OR api_key_enc IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_assistant_accounts_unit_alive ON assistant_accounts (unit_id) WHERE deleted_at IS NULL;
COMMENT ON TABLE  assistant_accounts IS 'AI yordamchi hisobi (provayder, model, kalit) — birlikka biriktiriladi, pastga meros bo''ladi';
COMMENT ON COLUMN assistant_accounts.provider IS 'none = bu birlik (va ostidagilar) yordamchidan foydalanmaydi — yuqoridagi hisob ham ishlatilmaydi';

CREATE TABLE IF NOT EXISTS assistant_threads (
  id               uuid         PRIMARY KEY DEFAULT uuidv7(),
  employee_id      uuid         NOT NULL REFERENCES employees(id),
  unit_id          uuid         REFERENCES org_units(id),           -- the scope the thread was opened in
  title            varchar(200) NOT NULL DEFAULT '',
  last_message_at  timestamptz,
  created_at       timestamptz  NOT NULL DEFAULT now(),
  updated_at       timestamptz  NOT NULL DEFAULT now(),
  deleted_at       timestamptz,
  deleted_by       uuid
);
CREATE INDEX IF NOT EXISTS ix_assistant_threads_employee ON assistant_threads (employee_id, last_message_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS assistant_messages (
  id               uuid         PRIMARY KEY DEFAULT uuidv7(),
  thread_id        uuid         NOT NULL REFERENCES assistant_threads(id) ON DELETE CASCADE,
  role             varchar(10)  NOT NULL,                          -- user | assistant | tool
  content          text         NOT NULL DEFAULT '',
  tool_calls       jsonb,                                          -- assistant: [{id, name, args}]
  tool_call_id     varchar(80),                                    -- tool: which call this answers
  tool_name        varchar(60),
  is_error         boolean      NOT NULL DEFAULT false,
  action_id        uuid,                                           -- the pending / resolved write this turn carries
  usage            jsonb,                                          -- provider usage of the assistant turn
  created_at       timestamptz  NOT NULL DEFAULT now(),
  updated_at       timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT ck_assistant_messages_role CHECK (role IN ('user', 'assistant', 'tool'))
);
CREATE INDEX IF NOT EXISTS ix_assistant_messages_thread ON assistant_messages (thread_id, created_at);

CREATE TABLE IF NOT EXISTS assistant_actions (
  id               uuid         PRIMARY KEY DEFAULT uuidv7(),
  thread_id        uuid         NOT NULL REFERENCES assistant_threads(id) ON DELETE CASCADE,
  employee_id      uuid         NOT NULL REFERENCES employees(id),
  tool_call_id     varchar(80)  NOT NULL,
  tool             varchar(60)  NOT NULL,
  title            varchar(300) NOT NULL,
  args             jsonb        NOT NULL DEFAULT '{}'::jsonb,
  status           varchar(12)  NOT NULL DEFAULT 'pending',        -- pending | done | failed | cancelled | expired
  result           jsonb,
  error            text,
  expires_at       timestamptz  NOT NULL,
  resolved_at      timestamptz,
  created_at       timestamptz  NOT NULL DEFAULT now(),
  updated_at       timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT ck_assistant_actions_status CHECK (status IN ('pending', 'done', 'failed', 'cancelled', 'expired'))
);
CREATE INDEX IF NOT EXISTS ix_assistant_actions_thread ON assistant_actions (thread_id, created_at);

-- every system role may talk to the assistant; what it may do stays gated by the role's other keys
UPDATE roles
   SET permissions = array_append(permissions, 'assistant.use'), updated_at = now()
 WHERE scope_unit_id IS NULL AND is_system AND deleted_at IS NULL
   AND NOT ('assistant.use' = ANY(permissions));
