#!/usr/bin/env bash
# =============================================================================
# Create the ACHR database and its two roles, idempotently.
#
# Why this exists
# ---------------
# On 2026-08-22 the `erp` database vanished from the cluster and every single
# endpoint began returning 500 -- including /healthz, which touches no database
# at all, because ATOMIC_REQUESTS=True opens a transaction before every view.
# Recreating it by hand is a sequence of six statements in a specific order; get
# the order wrong and you end up with tables owned by `postgres`, which is the
# one ownership that makes Row-Level Security inert (rolsuper skips policy
# evaluation entirely). This script encodes the order.
#
# Safe to re-run: every step checks before it acts. It will NOT touch an
# existing database's data.
#
#   sudo -u postgres bash scripts/db_bootstrap.sh
# =============================================================================
set -euo pipefail

ENV_FILE="${ENV_FILE:-/srv/achr/backend/.env}"
[[ -r "$ENV_FILE" ]] || { echo "FATAL: cannot read $ENV_FILE" >&2; exit 1; }

# Read only the keys we need. Never `source` the file: a stray shell
# metacharacter in a password would execute.
get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1 | tr -d '"'"'"'\r'; }

DB="$(get POSTGRES_DB)"
APP_USER="$(get POSTGRES_APP_USER)"
APP_PASS="$(get POSTGRES_APP_PASSWORD)"
: "${DB:?POSTGRES_DB missing from $ENV_FILE}"
: "${APP_USER:?POSTGRES_APP_USER missing from $ENV_FILE}"
: "${APP_PASS:?POSTGRES_APP_PASSWORD missing from $ENV_FILE}"

MIGRATOR="${MIGRATOR_USER:-erp_migrator}"
MIGRATOR_PASS="${MIGRATOR_PASSWORD:-}"
if [[ -z "$MIGRATOR_PASS" ]]; then
  MIGRATOR_PASS="$(openssl rand -base64 24 | tr -d '/+=')"
  GENERATED=1
fi

psql_su() { psql -v ON_ERROR_STOP=1 -qtA "$@"; }
exists_role() { [[ "$(psql_su -c "SELECT 1 FROM pg_roles WHERE rolname='$1'")" == "1" ]]; }
exists_db()   { [[ "$(psql_su -c "SELECT 1 FROM pg_database WHERE datname='$1'")" == "1" ]]; }

echo "==> database=$DB  app_role=$APP_USER  owner=$MIGRATOR"

# --- roles ------------------------------------------------------------------
# The owner. CREATEDB so the pytest suite can build test_<db> as this role.
if exists_role "$MIGRATOR"; then
  echo "    role $MIGRATOR already exists - leaving its password alone"
else
  psql_su -c "CREATE ROLE $MIGRATOR LOGIN PASSWORD '$MIGRATOR_PASS'
              NOSUPERUSER NOBYPASSRLS NOCREATEROLE CREATEDB INHERIT;"
  echo "    created role $MIGRATOR"
  [[ -n "${GENERATED:-}" ]] && echo "    !! MIGRATOR PASSWORD: $MIGRATOR_PASS  (store this now)"
fi

# The runtime role. NOBYPASSRLS is the load-bearing word: with rolbypassrls the
# tenant policies are decorative and any tenant can read any other's ledger.
if exists_role "$APP_USER"; then
  psql_su -c "ALTER ROLE $APP_USER LOGIN PASSWORD '$APP_PASS' NOSUPERUSER NOBYPASSRLS;"
  echo "    role $APP_USER exists - password re-synced with .env, NOBYPASSRLS enforced"
else
  psql_su -c "CREATE ROLE $APP_USER LOGIN PASSWORD '$APP_PASS'
              NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB INHERIT;"
  echo "    created role $APP_USER"
fi

# --- database ---------------------------------------------------------------
if exists_db "$DB"; then
  echo "    database $DB already exists - not touching it"
else
  psql_su -c "CREATE DATABASE $DB OWNER $MIGRATOR;"
  echo "    created database $DB owned by $MIGRATOR"
fi

# --- schema grants ----------------------------------------------------------
# REVOKE first: without it every role in the cluster can create objects in
# public and the ownership model above is decorative.
psql_su -d "$DB" <<SQL
ALTER DATABASE $DB OWNER TO $MIGRATOR;
ALTER SCHEMA public OWNER TO $MIGRATOR;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO $APP_USER;
GRANT ALL   ON SCHEMA public TO $MIGRATOR;
GRANT CONNECT ON DATABASE $DB TO $APP_USER;
ALTER DEFAULT PRIVILEGES FOR ROLE $MIGRATOR IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO $APP_USER;
ALTER DEFAULT PRIVILEGES FOR ROLE $MIGRATOR IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO $APP_USER;
ALTER DATABASE $DB SET "app.app_role" = '$APP_USER';
SQL
echo "    grants and default privileges applied"

# --- guard ------------------------------------------------------------------
# Assert rather than assume: a superuser app role is the failure mode this
# whole file exists to prevent, and it is invisible from the UI.
BYPASS="$(psql_su -c "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname='$APP_USER'")"
if [[ "$BYPASS" != "f" ]]; then
  echo "FATAL: $APP_USER can bypass RLS. Tenant isolation would be OFF. Aborting." >&2
  exit 1
fi
echo "==> OK. $APP_USER cannot bypass RLS."
echo "    Next: run migrations as $MIGRATOR, then restart achr.service"
