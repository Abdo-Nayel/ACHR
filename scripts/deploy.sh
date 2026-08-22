#!/usr/bin/env bash
# =============================================================================
# Deploy ACHR to the server. One command, ordered, and it verifies before and
# after rather than hoping.
#
# Why this exists
# ---------------
# Deployment was a remembered sequence of shell commands. A remembered sequence
# skips steps: on 2026-08-22 the site was down for hours because the database
# had gone missing and nothing checked, and because there was no single command
# that would have caught it. The two guards that matter here are the ones that
# run BEFORE anything is changed:
#
#   * `manage.py check --database default` fails loudly if the database is
#     unreachable or missing, so we never restart a service into an outage.
#   * `migrate --check` tells us whether migrations are pending, so a deploy
#     that needs them cannot silently skip them.
#
# Usage (as the service user, not root):
#   bash scripts/deploy.sh
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/achr}"
BACKEND="$APP_DIR/backend"
VENV="${VENV:-$BACKEND/.venv}"
SERVICE="${SERVICE:-achr.service}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8801/healthz}"
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings.prod}"

PY="$VENV/bin/python"
cd "$BACKEND"

step() { printf '\n==> %s\n' "$1"; }

step "Pulling latest main"
git -C "$APP_DIR" fetch origin --prune
git -C "$APP_DIR" pull --ff-only origin main

step "Installing dependencies"
"$VENV/bin/pip" install -q -r requirements.txt 2>/dev/null \
  || "$VENV/bin/pip" install -q -e .

# ---- pre-flight: fail here, before anything is changed ----------------------
step "Checking the database is actually there"
if ! "$PY" manage.py check --database default; then
  cat >&2 <<'MSG'
FATAL: the database is unreachable.

Do NOT restart the service - it would come up serving 500s on every endpoint
(ATOMIC_REQUESTS opens a transaction before every view, so even /healthz dies).

If the database is missing entirely:
    sudo -u postgres bash scripts/db_bootstrap.sh
MSG
  exit 1
fi

step "Applying migrations"
if "$PY" manage.py migrate --check >/dev/null 2>&1; then
  echo "    none pending"
else
  "$PY" manage.py migrate --noinput
fi

step "Collecting static files"
"$PY" manage.py collectstatic --noinput >/dev/null

step "Restarting $SERVICE"
sudo systemctl restart "$SERVICE"

# ---- post-flight: prove it actually came up --------------------------------
step "Verifying"
for i in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" || true)"
  if [[ "$code" == "200" ]]; then
    echo "    healthz OK ($(git -C "$APP_DIR" rev-parse --short HEAD))"
    exit 0
  fi
  sleep 2
done

echo "FATAL: healthz did not return 200 after 30s (last: ${code:-no response})" >&2
echo "       sudo journalctl -u $SERVICE -n 80 --no-pager" >&2
exit 1
