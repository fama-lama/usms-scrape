#!/bin/bash
# End-to-end test of famalama/usms-scrape:v0.15.10 against the LIVE portal.
# Uses the production stack's credentials (never printed), a 60s interval, and
# stops the container with SIGTERM afterwards to prove graceful shutdown.
set -uo pipefail
cd /opt/stacks/usms-scrape-stack || exit 1

umask 077
docker inspect usms-scrape --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E '^(USMS_USERNAME|USMS_PASSWORD|MQTT_USERNAME|MQTT_PASSWORD)=' > /tmp/e2e.env
# Host networking: reach the published selenium port on 127.0.0.1
{
  echo "SELENIUM_HOST=127.0.0.1"
  echo "SELENIUM_PORT=4444"
  echo "MQTT_BROKER=127.0.0.1"
  echo "MQTT_PORT=1883"
  echo "SCRAPE_INTERVAL=60"
} >> /tmp/e2e.env

docker rm -f usms_e2e >/dev/null 2>&1
docker run -d --name usms_e2e --network host --env-file /tmp/e2e.env \
  famalama/usms-scrape:v0.15.10 >/dev/null || { echo "RUN FAILED"; exit 1; }

# Wait for the first cycle to finish (it logs "sleeping" at the end of a cycle).
for i in $(seq 1 60); do
  if docker logs usms_e2e 2>&1 | grep -q "sleeping"; then break; fi
  sleep 2
done

echo "=== CYCLE LOG (structured lines) ==="
docker logs usms_e2e 2>&1 | grep -vE 'pthread_setaffinity' | sed -n '1,40p'

echo "=== ORT affinity warning count: $(docker logs usms_e2e 2>&1 | grep -c pthread_setaffinity) ==="

echo "=== MQTT RETAINED VALUES (home/usms/#) ==="
set -a; . /tmp/e2e.env; set +a
timeout 8 mosquitto_sub -h 127.0.0.1 -u "${MQTT_USERNAME}" -P "${MQTT_PASSWORD}" \
  -t 'home/usms/#' -v -W 4 2>&1 | sort

echo "=== SIGTERM (docker stop) ==="
timeout 20 docker stop usms_e2e >/dev/null 2>&1
echo "stop exit=$?"
docker logs usms_e2e 2>&1 | tail -4
docker inspect usms_e2e --format 'ExitCode={{.State.ExitCode}} OOM={{.State.OOMKilled}}' 2>/dev/null
docker rm -f usms_e2e >/dev/null 2>&1
rm -f /tmp/e2e.env
echo "=== CLEANED UP ==="
