-- ============================================================================
-- TemoDB v2 · 03 · staff: roles, employees, scope, sessions, OTP
--
-- Scope model: an employee belongs to ONE home unit (any level) and may get
-- extra units (employee_units). Their effective scope = union of the subtrees.
--   • tuman xodimi  → unit = muassasa          → faqat o'z muassasasi
--   • viloyat rahbari → unit = viloyat tuguni  → viloyatdagi barcha tuman/muassasalar
--   • respublika    → unit = davlat tuguni
--   • superadmin    → unit NULL, is_super_admin = true (hamma narsa)
-- Roles are permission bundles only; WHERE they apply comes from the unit.
-- ============================================================================

CREATE TABLE roles (
  id            uuid         PRIMARY KEY DEFAULT uuidv7(),
  scope_unit_id uuid         REFERENCES org_units(id),   -- NULL = tizim roli (hamma uchun), aks holda shu birlik va ostida
  key           varchar(60)  NOT NULL,
  name          varchar(120) NOT NULL,
  description   text,
  permissions   text[]       NOT NULL DEFAULT '{}',
  is_system     boolean      NOT NULL DEFAULT false,
  created_by    uuid,
  created_at    timestamptz  NOT NULL DEFAULT now(),
  updated_at    timestamptz  NOT NULL DEFAULT now(),
  deleted_at    timestamptz,
  deleted_by    uuid,
  CONSTRAINT ck_roles_key_format CHECK (key ~ '^[a-z][a-z0-9_-]{1,59}$')
);
CREATE UNIQUE INDEX uq_roles_scope_key_alive
  ON roles (COALESCE(scope_unit_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(key))
  WHERE deleted_at IS NULL;
CREATE INDEX ix_roles_scope ON roles (scope_unit_id);
COMMENT ON TABLE  roles               IS 'Rollar = ruxsatlar to''plami. scope_unit_id NULL → tizim roli; aks holda shu birlik daraxtida ishlatiladi.';
COMMENT ON COLUMN roles.permissions   IS '<module>.<resource>.<action> kalitlari (app/core/permissions.py katalogi)';

CREATE TABLE employees (
  id                  uuid         PRIMARY KEY DEFAULT uuidv7(),
  unit_id             uuid         REFERENCES org_units(id),   -- asosiy birlik (superadmin uchun NULL)
  role_id             uuid         NOT NULL REFERENCES roles(id),
  full_name           varchar(200) NOT NULL,
  login               varchar(80)  NOT NULL,
  password_hash       text,
  phone               varchar(40),
  email               varchar(200),
  overrides           jsonb        NOT NULL DEFAULT '{"allow": [], "deny": []}'::jsonb,
  category_ids        uuid[]       NOT NULL DEFAULT '{}',      -- laborant/vrach xizmat ko'rsatadigan kategoriyalar
  status              varchar(20)  NOT NULL DEFAULT 'active',
  avatar_hue          smallint     NOT NULL DEFAULT 160,
  is_super_admin      boolean      NOT NULL DEFAULT false,
  last_login_at       timestamptz,
  failed_logins       smallint     NOT NULL DEFAULT 0,
  locked_until        timestamptz,
  signature_asset_id  uuid,                                    -- FK → template_assets (05_catalog)
  created_by          uuid,
  created_at          timestamptz  NOT NULL DEFAULT now(),
  updated_at          timestamptz  NOT NULL DEFAULT now(),
  deleted_at          timestamptz,
  deleted_by          uuid,
  CONSTRAINT ck_employees_scope     CHECK (is_super_admin OR unit_id IS NOT NULL),
  CONSTRAINT ck_employees_status    CHECK (status IN ('active', 'inactive')),
  CONSTRAINT ck_employees_login     CHECK (length(login) >= 3 AND login !~ '\s'),
  CONSTRAINT ck_employees_hue       CHECK (avatar_hue BETWEEN 0 AND 360),
  CONSTRAINT ck_employees_overrides CHECK (jsonb_typeof(overrides) = 'object')
);
CREATE UNIQUE INDEX uq_employees_login_alive ON employees (lower(login)) WHERE deleted_at IS NULL;
CREATE INDEX ix_employees_unit_status    ON employees (unit_id, status);
CREATE INDEX ix_employees_role           ON employees (role_id);
CREATE INDEX ix_employees_fold_name_trgm ON employees USING gin (fold_text(full_name) gin_trgm_ops);
COMMENT ON TABLE  employees                IS 'Xodimlar. unit_id = asosiy birlik (ko''lam = shu birlik va ostidagilar).';
COMMENT ON COLUMN employees.is_super_admin IS 'Platforma superadmini — birlikka bog''lanmaydi, hamma narsani ko''radi';
COMMENT ON COLUMN employees.overrides      IS '{"allow": [perm…], "deny": [perm…]} — rolga qo''shimcha / olib tashlash';

-- extra scope units (an employee serving two facilities in a district, etc.)
CREATE TABLE employee_units (
  employee_id uuid NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
  unit_id     uuid NOT NULL REFERENCES org_units(id),
  PRIMARY KEY (employee_id, unit_id)
);
CREATE INDEX ix_employee_units_unit ON employee_units (unit_id);
COMMENT ON TABLE employee_units IS 'Xodimning qo''shimcha birliklari (asosiy unit_id dan tashqari)';

-- effective scope: every unit the employee may act in
CREATE OR REPLACE FUNCTION employee_scope(emp uuid) RETURNS SETOF uuid
LANGUAGE sql STABLE AS $$
  SELECT DISTINCT c.descendant_id
  FROM org_unit_closure c
  WHERE c.ancestor_id IN (
    SELECT unit_id FROM employees WHERE id = emp AND unit_id IS NOT NULL
    UNION
    SELECT unit_id FROM employee_units WHERE employee_id = emp)
$$;
COMMENT ON FUNCTION employee_scope(uuid) IS 'Xodim ko''ra oladigan barcha birliklar (asosiy + qo''shimcha birliklar daraxtlari)';

CREATE OR REPLACE FUNCTION employee_can_see(emp uuid, unit uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT COALESCE((SELECT is_super_admin FROM employees WHERE id = emp), false)
      OR EXISTS (SELECT 1 FROM employee_scope(emp) s WHERE s = unit)
$$;

-- ---------------------------------------------------------------------------
-- sessions (jti allow-list) and OTP challenges
-- ---------------------------------------------------------------------------
CREATE TABLE sessions (
  id           varchar(40)  PRIMARY KEY,                  -- jti
  actor        varchar(10)  NOT NULL,
  method       varchar(10),                               -- patient: otp | login ; staff: password
  subject_id   uuid         NOT NULL,
  unit_id      uuid         REFERENCES org_units(id),     -- sessiya ochilgan birlik (superadmin / bemor uchun NULL bo'lishi mumkin)
  expires_at   timestamptz  NOT NULL,
  revoked_at   timestamptz,
  ip           varchar(64),
  user_agent   varchar(300),
  created_at   timestamptz  NOT NULL DEFAULT now(),
  last_seen_at timestamptz,
  CONSTRAINT ck_sessions_actor  CHECK (actor IN ('staff', 'patient')),
  CONSTRAINT ck_sessions_method CHECK (method IS NULL OR method IN ('otp', 'login', 'password'))
);
CREATE INDEX ix_sessions_subject ON sessions (subject_id);
CREATE INDEX ix_sessions_expires ON sessions (expires_at);
COMMENT ON TABLE sessions IS 'Berilgan tokenlar (jti): bekor qilish va audit uchun';

CREATE TABLE otp_challenges (
  id           uuid         PRIMARY KEY DEFAULT uuidv7(),
  phone        varchar(20)  NOT NULL,
  purpose      varchar(20)  NOT NULL,                     -- portal | telegram | document
  code_hash    varchar(64)  NOT NULL,
  attempts     smallint     NOT NULL DEFAULT 0,
  max_attempts smallint     NOT NULL DEFAULT 3,
  expires_at   timestamptz  NOT NULL,
  consumed_at  timestamptz,
  unit_id      uuid         REFERENCES org_units(id),
  meta         jsonb        NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT ck_otp_attempts CHECK (attempts >= 0 AND max_attempts > 0)
);
CREATE INDEX ix_otp_phone   ON otp_challenges (phone);
CREATE INDEX ix_otp_expires ON otp_challenges (expires_at);
COMMENT ON TABLE otp_challenges IS 'SMS kodlar (portal kirishi, Telegram ulash, hujjat ochish)';

SELECT attach_updated_at_triggers();
