-- ============================================================================
-- TemoDB v2 · 38 · one phone, a whole family
--   Patients without a passport / PINFL may now share a phone number: the unique index on (country, phone)
--   for such patients goes away. Every person added to a number that already belongs to someone says who they
--   are to that person — `relation` (child, spouse, father, mother, brother_older / _younger, sister_older /
--   _younger, grandchild, grandparent, relative, other) to `related_patient_id`. The API keeps the rules
--   (a relation is required for an undocumented second person; the same name + birth date on the number is
--   the same person). Idempotent: re-running changes nothing.
-- ============================================================================

DROP INDEX IF EXISTS uq_patients_country_phone_noid_alive;

ALTER TABLE patients ADD COLUMN IF NOT EXISTS related_patient_id uuid REFERENCES patients(id);
ALTER TABLE patients ADD COLUMN IF NOT EXISTS relation varchar(20);

ALTER TABLE patients DROP CONSTRAINT IF EXISTS ck_patients_relation;
ALTER TABLE patients ADD CONSTRAINT ck_patients_relation CHECK (
  relation IS NULL OR relation IN ('child', 'spouse', 'father', 'mother', 'brother_older', 'brother_younger',
                                   'sister_older', 'sister_younger', 'grandchild', 'grandparent', 'relative', 'other'));
ALTER TABLE patients DROP CONSTRAINT IF EXISTS ck_patients_relation_pair;
ALTER TABLE patients ADD CONSTRAINT ck_patients_relation_pair CHECK ((relation IS NULL) = (related_patient_id IS NULL));
ALTER TABLE patients DROP CONSTRAINT IF EXISTS ck_patients_relation_self;
ALTER TABLE patients ADD CONSTRAINT ck_patients_relation_self CHECK (related_patient_id IS NULL OR related_patient_id <> id);

CREATE INDEX IF NOT EXISTS ix_patients_related ON patients (related_patient_id) WHERE related_patient_id IS NOT NULL;

COMMENT ON COLUMN patients.relation IS 'Bu bemor related_patient_id ga kim: child | spouse | father | mother | brother_older | brother_younger | sister_older | sister_younger | grandchild | grandparent | relative | other';
COMMENT ON COLUMN patients.related_patient_id IS 'Qaysi bemorga nisbatan (odatda telefon raqam egasi)';
