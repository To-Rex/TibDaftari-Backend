#!/usr/bin/env bash
# TemoDB v2 — apply every NN_*.sql in order, each in its own transaction, once.
# Runs INSIDE the postgres container:  bash /tmp/temodb_v2/run.sh
set -euo pipefail
cd "$(dirname "$0")"
export PGPASSWORD="${PGPASSWORD:-toor}"
DB="${TEMODB_NAME:-TemoDB}"
PSQL=(psql -U postgres -d "$DB" -v ON_ERROR_STOP=1 -P pager=off -q -X)

has_registry() { "${PSQL[@]}" -tAc "select 1 from pg_tables where schemaname='public' and tablename='schema_migrations'" | grep -q 1; }

for f in $(ls [0-9][0-9]_*.sql | sort); do
  v="${f%%_*}"; name="${f#*_}"; name="${name%.sql}"
  sum=$(sha256sum "$f" | cut -c1-64)
  if has_registry && "${PSQL[@]}" -tAc "select 1 from schema_migrations where version='$v'" | grep -q 1; then
    echo "skip   $f"; continue
  fi
  echo "apply  $f"
  "${PSQL[@]}" --single-transaction -f "$f"
  "${PSQL[@]}" -c "insert into schema_migrations(version, name, checksum) values ('$v', '$name', '$sum')"
done
echo "done"
