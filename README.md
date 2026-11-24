
## USMS Scraper BRUNEI 
<img width="64" height="64" alt="usms" src="https://github.com/user-attachments/assets/e4e2fae5-b3ea-4b08-8b51-c8b1a5f4fdbf" /> 

This project scrapes your USMS Smart Meter dashboard data using Selenium WebDriver and optionally publishes updates to an MQTT broker. It runs continuously at configurable intervals.

Only works for smart meters registered with https://www.usms.com.bn/smartmeter/index.html


---

## Features

- Logs into the USMS Smart Meter website  
- Scrapes remaining units, balance, and meter last polled time  
- Optionally publishes data to MQTT topics  
- Runs continuously with configurable scrape interval  
- **Robust login**: Uses `WebDriverWait` instead of blind `time.sleep()` — waits for the dashboard to actually load
- **Captcha login**: The site's second login stage asks for a captcha. It is read locally by `ddddocr` and only submitted when 4 of 5 OCR passes agree; a disagreement just re-rolls a fresh code, which costs nothing
- **Smart failure handling**: Publishes only the fields that validated — a partial scrape never overwrites a good Home Assistant sensor with the text `N/A`. Skipped publishes keep the last retained value, and `home/usms/last_run` only advances on a fully successful cycle, so Home Assistant can alert on a *stale* scraper instead of a dead one
- **Auto-heal**: Any 3 consecutive failed cycles (portal, captcha, Selenium — not just `N/A` results) exit the container so Docker's `restart: unless-stopped` recycles it cleanly
- **Value checks**: Every field is shape-checked (`… kWh`, an amount, `dd/mm/yyyy`) before it is published — a shifted table row becomes a warning instead of silently publishing the wrong cell
- **Retained + QoS 1**: Publishes are retained, so a Home Assistant restart recovers the last values instantly; `home/usms/status` (`ok` / `partial` / `fail`, plus an `offline` last-will) reports scraper health
- **Debug friendly**: Logs page title, URL and a digit-masked body preview on failure, saves the full page to `/tmp` for layout work, and ends every cycle with one greppable line —
  `cycle=dc74 result=ok unit='1987.599 kWh' balance='$67.12' polled='24/11/2026' captcha_rolls=1 driver=0.2s login=12.9s scrape=0.1s publish=1.0s total=14.2s`.
  `LOG_LEVEL=DEBUG` adds per-navigation timings with page titles, every captcha reading with its inference time, **why** a roll was rejected (`shape: 4 chars, expected 5; agreement 3 < 4`), per-field read latency and per-topic MQTT ack times
- **Fresh session per cycle**: Creates clean Chrome options and cleans up WebDriver connections every cycle to prevent long-running connection leaks
- **Quiet logs on any host**: ONNX Runtime's CPU thread affinity is disabled with explicit thread counts, so containers with a restricted or scattered CPU set (LXC coresets, `--cpuset-cpus`, Kubernetes, WSL2) no longer print `pthread_setaffinity_np failed … error code: 22` on every start

---

## Environment Variables

| Variable         | Description                                                                                     | Required | Default       |
|------------------|-------------------------------------------------------------------------------------------------|----------|---------------|
| `USMS_USERNAME`  | Your USMS Smart Meter account username (Brunei IC number)                                       | Yes      | N/A           |
| `USMS_PASSWORD`  | Your USMS Smart Meter account password                                                         | Yes      | N/A           |
| `SELENIUM_HOST`  | Hostname or IP where Selenium WebDriver (e.g., Selenium Grid or standalone) is running          | Yes       | `localhost`   |
| `SELENIUM_PORT`  | Port where Selenium WebDriver is listening                                                     | Yes       | `4444`        |
| `MQTT_BROKER`    | MQTT broker hostname or IP address (MQTT is enabled only when this is set)                      | No       | N/A           |
| `MQTT_PORT`      | MQTT broker port                                                                               | No       | `1883`        |
| `MQTT_USERNAME`  | Username for MQTT broker authentication (optional — anonymous brokers work without it)          | No       | N/A           |
| `MQTT_PASSWORD`  | Password for MQTT broker authentication (optional)                                             | No       | N/A           |
| `SCRAPE_INTERVAL`| Time interval between scraping runs in seconds (integer, minimum `60`)                          | Yes       | `1800` (30 min)|
| `LOG_LEVEL`      | `DEBUG`, `INFO`, `WARNING`, `ERROR` — applies to the scraper's own log lines                    | No       | `INFO`        |
| `USMS_TIMEZONE`  | IANA timezone for the `last_run` timestamp                                                     | No       | `Asia/Brunei` |

`LOG_LEVEL=DEBUG` deliberately does **not** raise the Selenium/urllib3 loggers: their
per-request debug records contain the account password and IC number.

---


## MQTT Topics Published

- `home/usms/remaining_unit` — Remaining units on your smart meter  
- `home/usms/remaining_balance` — Remaining balance in your account  
- `home/usms/meter_last_polled` — Last meter polling timestamp  
- `home/usms/last_run` — Timestamp of the last **fully successful** scrape cycle  
- `home/usms/status` — `ok` (all three fields), `partial` (some), `fail` (none), `offline` (last will)

All topics are published with `retain=True, qos=1`, so a subscriber that starts late still
receives the last values. A field that fails validation is simply not published — the
retained value from the previous good cycle stays put.

---

## Usage

### Prerequisites

- USMS account registered to smart meter
- Docker Compose

  
- (Optional) MQTT broker accessible for publishing updates
  (note: without MQTT broker, information is printed into log only.)

### Running with Docker Compose

1. Edit your `docker-compose.yml` to include:

```yaml
services:
  usms-scrape:
    image: famalama/usms-scrape:latest
    restart: unless-stopped          # survive a host reboot and the built-in auto-heal exit
    container_name: usms-scrape
    environment:
      - USMS_USERNAME=01234567 #CHANGE (your IC number, as used on the USMS portal)
      - USMS_PASSWORD=PASSWORD123! #CHANGE (your USMS portal password)
      - SELENIUM_HOST=selenium        # must match the browser service name below
      - SELENIUM_PORT=4444
      # The four MQTT_* lines are optional — delete them all if you do not publish to MQTT.
      - MQTT_BROKER=x.x.x.x #CHANGE
      - MQTT_PORT=1883 #CHANGE
      - MQTT_USERNAME=mqttuser #CHANGE
      - MQTT_PASSWORD=mqttpassword #CHANGE
      - SCRAPE_INTERVAL=1800          # seconds between scrapes (1800 = every 30 min)
    depends_on:
      selenium:
        condition: service_healthy    # do not start until Chrome answers /wd/hub/status
  selenium:
    image: selenium/standalone-chrome:latest
    container_name: selenium
    healthcheck:                      # every scrape waits on this check
      test:
        - CMD
        - curl
        - -f
        - http://localhost:4444/wd/hub/status
      interval: 5s
      timeout: 2s
      retries: 10
    restart: unless-stopped
    ports:
      - 4444:4444                     # Selenium WebDriver endpoint (required)
      # OPTIONAL — live view of the browser while it works, useful for seeing where
      # a login or a scrape gets stuck. Uncomment to watch http://<docker-host>:7900
      # (password defaults to "secret" — see "Watching it run" below).
      # Not needed for normal operation.
      # - 7900:7900
    # OPTIONAL — your own password for that live view. The image default is "secret";
    # leave both lines commented out to keep the default.
    # environment:
    #   - SE_VNC_PASSWORD=change-me
networks: {}

```

2. Start the container:

```
docker compose up -d
```

3. Follow the first cycle to make sure it worked:

```
docker compose logs -f usms-scrape
```

A healthy cycle logs `Login successful`, the three scraped values, and `Published to MQTT.`. A captcha that the OCR cannot read confidently is re-rolled and logged as `Captcha attempt N/10`; if all rolls fail the run is skipped and retried on the next interval, without overwriting your last good sensor values.

The scraper will run automatically, scrape your USMS data every SCRAPE_INTERVAL seconds, and publish to MQTT if configured.

---

## Watching it run (optional)

The scraper drives a real Chrome, so when a login or a scrape looks stuck you can watch the browser instead of guessing from logs.

1. Uncomment `- 7900:7900` in the `selenium` service above and apply it: `docker compose up -d selenium`
2. Open `http://<docker-host>:7900`
3. Enter the VNC password — the image default is `secret`, or whatever you set in `SE_VNC_PASSWORD`

The same browser is also listed in the Selenium Grid UI at `http://<docker-host>:4444/ui`.

Notes:

- Both extras are optional. Closed ports are the default and the scraper never needs them.
- `SE_VNC_PASSWORD` protects this live view only — it is not your USMS password.
- If you publish `7900`, keep it LAN-only or behind a VPN. Anyone who can reach it can watch and type into a browser that is logged into your meter account.
- The live view shows the browser, not the OCR: captcha reads are logged as `Captcha attempt N/10: '<code>' (n/5 models agree)`.

## Home Assistant sensors

insert this code into your configuration.yaml

```yaml
mqtt:
  sensor:
    # Remaining credit on the meter, published as "2003.508 kWh" -> 2003.508
    - name: USMS Remaining Unit
      state_topic: home/usms/remaining_unit
      value_template: "{{ value.split(' ')[0] }}"
      unit_of_measurement: "kWh"
      state_class: total

    # Account balance, published as "$67.28" -> 67.28
    - name: USMS Remaining Balance
      state_topic: home/usms/remaining_balance
      value_template: "{{ value.replace('$', '') }}"
      unit_of_measurement: "BND"
      state_class: total

    # Date the utility last read the meter (DD/MM/YYYY) — compare with today to spot a stale meter
    - name: USMS Meter Last Polled
      state_topic: home/usms/meter_last_polled
      value_template: "{{ value }}"


    # When the scraper last succeeded — stays put when a run fails, so a gap here means the scraper is stuck
    - name: USMS Last Run Time
      state_topic: home/usms/last_run
      value_template: "{{ value | as_datetime('%Y-%m-%d %H:%M:%S') }}"
```

## Development

The main scraper script is usms.py.

Uses Selenium for browser automation and scraping. 

Environment variables control behavior and credentials.

Login is two stages: `ResLogin` takes the IC and password, then hands over to `ResLogin?sc=…`, which asks for both again plus a captcha. The code is read by `ddddocr` (CPU-only ONNX model — no GPU, no network call and no human input), and the captcha image is read out of Chrome's own cache because its URL is single-use.

## License

This project is licensed under the GNU General Public License v3.0 (GPLv3). See LICENSE for details.
