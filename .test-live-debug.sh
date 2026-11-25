#!/bin/bash
# Live INFO/DEBUG demonstration against the real portal with the patched source.
# Also proves the DEBUG log never contains the account credentials.
set -uo pipefail
umask 077
docker inspect usms-scrape --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E '^(USMS_USERNAME|USMS_PASSWORD|MQTT_USERNAME|MQTT_PASSWORD)=' > /tmp/ld.env
{ echo "SELENIUM_HOST=127.0.0.1"; echo "SELENIUM_PORT=4444"; echo "MQTT_BROKER=127.0.0.1";
  echo "MQTT_PORT=1883"; echo "SCRAPE_INTERVAL=60"; echo "LOG_LEVEL=DEBUG"; } >> /tmp/ld.env
set -a; . /tmp/ld.env; set +a

docker rm -f usms_ld >/dev/null 2>&1
docker run -d --name usms_ld --network host --env-file /tmp/ld.env \
  -v /tmp/usms-fw.py:/app/usms.py:ro famalama/usms-scrape:v0.15.10 >/dev/null || exit 1

for i in $(seq 1 60); do
  docker logs usms_ld 2>&1 | grep -q "sleeping" && break
  sleep 2
done

LOG=/tmp/ld.log
docker logs usms_ld > "$LOG" 2>&1
docker stop usms_ld >/dev/null 2>&1
docker rm -f usms_ld >/dev/null 2>&1

echo "=== LEVEL COUNTS"
grep -oE '\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]' "$LOG" | sort | uniq -c
echo
echo "=== INFO / WARNING / ERROR lines (one cycle)"
grep -E '\[(INFO|WARNING|ERROR)\]' "$LOG" | grep -v pthread | sed -n '1,40p'
echo
echo "=== sample DEBUG lines"
grep -E '\[DEBUG\]' "$LOG" | grep -v pthread | sed -n '1,25p'
echo
echo "=== CREDENTIAL LEAK CHECK"
echo "password occurrences: $(grep -cF -- "$USMS_PASSWORD" "$LOG" 2>/dev/null || echo 0)"
echo "username/IC occurrences: $(grep -cF -- "$USMS_USERNAME" "$LOG" 2>/dev/null || echo 0)"
echo "ort affinity lines: $(grep -c pthread_setaffinity "$LOG")"
rm -f /tmp/ld.env "$LOG"
