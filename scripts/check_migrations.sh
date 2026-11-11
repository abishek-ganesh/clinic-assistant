#!/usr/bin/env bash
# Apply every migration to a throwaway pgvector Postgres in Docker, then run the SQL assertions.
# Usage: scripts/check_migrations.sh   (needs Docker; leaves nothing running)
set -euo pipefail
cd "$(dirname "$0")/.."

# CHECK_NAME and CHECK_PORT let two checks run side by side
NAME=${CHECK_NAME:-clinic-migration-check}
PORT=${CHECK_PORT:-54329}
export PGPASSWORD=test
docker run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=test -p "$PORT:5432" pgvector/pgvector:pg17 >/dev/null
trap 'docker stop "$NAME" >/dev/null' EXIT

psql_() { psql -h localhost -p "$PORT" -U postgres -q -v ON_ERROR_STOP=1 "$@"; }
until psql_ -c "select 1" >/dev/null 2>&1; do sleep 1; done

psql_ -f supabase/tests/supabase_shim.sql
for migration in supabase/migrations/*.sql; do
  echo "applying $migration"
  psql_ -f "$migration"
done
psql_ -f supabase/tests/migration_checks.sql
for checks in supabase/tests/checks_*.sql; do
  [ -e "$checks" ] || continue
  echo "running $checks"
  psql_ -f "$checks"
done
echo "migration checks passed"
