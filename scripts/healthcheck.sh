#!/usr/bin/env bash
# =============================================================================
# Poll /healthz and shout if it is not 200.
#
# Why this exists
# ---------------
# The 2026-08-22 outage was discovered by opening the site in a browser. Nothing
# was watching. /healthz returns 200 only when Django can open a database
# transaction, so it catches exactly the failure that happened.
#
# Install:
#   */5 * * * * /srv/achr/scripts/healthcheck.sh >> /var/log/achr-health.log 2>&1
# =============================================================================
set -uo pipefail

URL="${HEALTH_URL:-https://achr.erpbylyomastech.com/healthz}"
STATE="${STATE_FILE:-/tmp/achr-health.state}"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL" || echo 000)"
last="$(cat "$STATE" 2>/dev/null || echo 200)"
echo "$code" > "$STATE"

if [[ "$code" == "200" ]]; then
  # Only announce the recovery, not every healthy poll.
  [[ "$last" != "200" ]] && echo "$(date -Is) RECOVERED: $URL is 200 again"
  exit 0
fi

echo "$(date -Is) DOWN: $URL returned $code"
# Alert once per outage, not every five minutes.
if [[ "$last" == "200" ]]; then
  logger -t achr-health -p daemon.err "ACHR is DOWN: $URL returned $code"
  # Add your own notification here (mail -s / curl to a webhook / etc.)
fi
exit 1
