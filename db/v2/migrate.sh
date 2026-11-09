#!/usr/bin/env bash
# TemoDB v2 — one-shot data migration orchestrator. Runs on the HOST (root@server).
#   1. pg_dump production (read-only) → file
#   2. restore it as database `legacy_tibdaftari` inside the TemoDB container
#   3. run.sh applies 10_ (DDL tweak), 29_ (fdw), 30_ (transform) into TemoDB
# Production is never written to. Re-running is safe: the legacy DB is recreated,
# already-applied scripts are skipped by run.sh.
set -euo pipefail
PROD_CT="data-base-ses-he6aad.1.8fa9tao4vyl3y2o9gyga1jh1a"
NEW_CT="data-base-temodb-ckaosy.1.2nmgv0jrodeygrxbr21iazh3k"
BUNDLE="/root/temodb_v2"
DUMP="$BUNDLE/legacy.dump"

echo "1/4 dumping production (custom format, read-only) …"
docker exec -e PGPASSWORD=toor "$PROD_CT" pg_dump -U postgres -d TibDaftari -Fc --no-owner --no-privileges -f /tmp/legacy.dump
docker cp "$PROD_CT:/tmp/legacy.dump" "$DUMP"
docker exec "$PROD_CT" rm -f /tmp/legacy.dump
ls -la "$DUMP"

echo "2/4 restoring the copy as legacy_tibdaftari inside the TemoDB instance …"
docker cp "$DUMP" "$NEW_CT:/tmp/legacy.dump"
docker exec -e PGPASSWORD=toor "$NEW_CT" psql -U postgres -d postgres -X -q -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS legacy_tibdaftari WITH (FORCE)" \
  -c "CREATE DATABASE legacy_tibdaftari TEMPLATE template0 ENCODING 'UTF8'"
docker exec -e PGPASSWORD=toor "$NEW_CT" pg_restore -U postgres -d legacy_tibdaftari --no-owner --no-privileges --exit-on-error /tmp/legacy.dump
docker exec -e PGPASSWORD=toor "$NEW_CT" psql -U postgres -d legacy_tibdaftari -X -tA -c "select 'legacy rows: orders='||(select count(*) from orders)||' patients='||(select count(*) from patients)||' files='||(select count(*) from stored_files)"

echo "3/4 syncing scripts into the container …"
docker cp "$BUNDLE" "$NEW_CT:/tmp/temodb_v2_new" && docker exec "$NEW_CT" bash -c 'rm -rf /tmp/temodb_v2 && mv /tmp/temodb_v2_new /tmp/temodb_v2'

echo "4/4 applying 10_/29_/30_ …"
docker exec -e PGPASSWORD=toor "$NEW_CT" bash /tmp/temodb_v2/run.sh
echo "migration finished"
