#!/bin/bash
# Test the patched usms.py end-to-end against the live portal, inside the same
# image, with the real credentials, without touching the production container.
set -uo pipefail

SRC="${1:-/tmp/usms_patched.py}"
CPUSET="${2:-}"          # empty = inherit the LXC coreset (production shape)
LABEL="${3:-prod-cpuset}"
LOG="/tmp/usms_test_${LABEL}.log"

umask 077
docker inspect usms-scrape --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E '^(USMS_USERNAME|USMS_PASSWORD|MQTT_USERNAME|MQTT_PASSWORD|MQTT_BROKER|MQTT_PORT)=' > /tmp/usms_test.env
echo "SELENIUM_HOST=127.0.0.1" >> /tmp/usms_test.env
echo "SELENIUM_PORT=4444" >> /tmp/usms_test.env
echo "SCRAPE_INTERVAL=600" >> /tmp/usms_test.env

EXTRA=()
[ -n "$CPUSET" ] && EXTRA=(--cpuset-cpus "$CPUSET")

timeout 180 docker run --rm --name "usms_test_${LABEL}" --network host \
  "${EXTRA[@]}" \
  -v "$SRC":/app/usms.py:ro \
  --env-file /tmp/usms_test.env \
  famalama/usms-scrape:stable > "$LOG" 2>&1
RC=$?

# `timeout` kills the docker CLI, not the container — stop it explicitly so the
# test run cannot keep scraping (and publishing) in the background.
docker stop "usms_test_${LABEL}" >/dev/null 2>&1
docker rm -f "usms_test_${LABEL}" >/dev/null 2>&1

rm -f /tmp/usms_test.env

echo "=== label=$LABEL cpuset=${CPUSET:-inherited} exit=$RC"
echo "affinity_warnings: $(grep -c 'pthread_setaffinity' "$LOG")"
echo "--- log:"
grep -vE 'pthread_setaffinity' "$LOG" | sed -n '1,40p'
