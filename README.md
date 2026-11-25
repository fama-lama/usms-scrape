
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
- **Value checks**: Every field is shape-checked (a reading with its unit — `… kWh` or `… m³` —, an amount, `dd/mm/yyyy`) before it is published — a shifted table row becomes a warning instead of silently publishing the wrong cell
- **Multi-meter accounts**: Every meter card on the dashboard is read — electricity, water or both — each published under its own `home/usms/<type>/…` topics, with the classic topics following the electricity meter so existing dashboards keep working
- **Self-installing Home Assistant entities**: For multi-meter (or water-only) accounts the scraper also publishes retained MQTT discovery configs, so HA creates the per-meter sensors and device itself — no YAML to copy
- **Credential handling**: `USMS_PASSWORD_FILE` / `USMS_USERNAME_FILE` for passwords Docker Compose would mangle (it interpolates `$` away and passes quotes through literally), plus startup warnings that name the problem instead of failing the login
- **Retained + QoS 1**: Publishes are retained, so a Home Assistant restart recovers the last values instantly; `home/usms/status` (`ok` / `partial` / `fail`, plus an `offline` last-will) reports scraper health
- **Debug friendly**: Logs page title, URL and a digit-masked body preview on failure, saves the full page to `/tmp` for layout work, and ends every cycle with one greppable line —
  `cycle=dc74 result=ok meters=1 unit='1987.599 kWh' balance='$67.12' polled='24/11/2026' captcha_rolls=1 driver=0.2s login=12.9s scrape=0.1s publish=1.0s total=14.2s`
  (on a multi-meter account `result=ok` requires **every** meter to come back complete; `meters=N` says how many were read).
  `LOG_LEVEL=DEBUG` adds per-navigation timings with page titles, every captcha reading with its inference time, **why** a roll was rejected (`shape: 4 chars, expected 5; agreement 3 < 4`), per-field read latency and per-topic MQTT ack times
- **Fresh session per cycle**: Creates clean Chrome options and cleans up WebDriver connections every cycle to prevent long-running connection leaks
- **Quiet logs on any host**: ONNX Runtime's CPU thread affinity is disabled with explicit thread counts, so containers with a restricted or scattered CPU set (LXC coresets, `--cpuset-cpus`, Kubernetes, WSL2) no longer print `pthread_setaffinity_np failed … error code: 22` on every start

---

## Environment Variables

| Variable         | Description                                                                                     | Required | Default       |
|------------------|-------------------------------------------------------------------------------------------------|----------|---------------|
| `USMS_USERNAME`  | Your USMS Smart Meter account username (Brunei IC number)                                       | Yes      | N/A           |
| `USMS_PASSWORD`  | Your USMS Smart Meter account password                                                         | Yes      | N/A           |
| `USMS_USERNAME_FILE` | Path to a file containing the username (Docker secret / bind mount). Used when `USMS_USERNAME` is unset. | No | N/A |
| `USMS_PASSWORD_FILE` | Path to a file containing the password (Docker secret / bind mount). Used when `USMS_PASSWORD` is unset. | No | N/A |
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

### Passwords containing `$`, quotes or spaces

Docker Compose interpolates `$` in **every** value it reads — `environment:`,
`env_file:` and `.env` alike. A password like `MyP@ss$w0rd` therefore reaches the
container as `MyP@ss` (`$w0rd` is substituted as an empty variable), which the portal
answers with the same "Incorrect user name and password" it gives a genuinely wrong
password. Three ways to get it right, in order of preference:

1. **A file instead of an environment value** — nothing is interpolated in front of it.
   Generate the file first, then edit one line in it:
   ```bash
   ./make-credentials.sh            # writes ./usms_pw, mode 600, with instructions inside
   $EDITOR usms_pw                  # replace the PASTE-YOUR-PASSWORD-HERE line
   ```
   ```yaml
   environment:
     - USMS_USERNAME=01234567
     - USMS_PASSWORD_FILE=/run/secrets/usms_pw   # absolute path INSIDE the container
   volumes:
     - ./usms_pw:/run/secrets/usms_pw:ro
   ```
   The generated file carries a comment header; the scraper ignores `#` lines and
   blanks and uses the first other line, so the instructions can stay in the file
   beside the password. Until the placeholder is replaced the container refuses to
   start and says so — a half-finished setup can never masquerade as a wrong
   password. Startup then logs `USMS_PASSWORD loaded from … (N characters)` and the
   config line shows `password=set (N chars)`; if N is shorter than what you typed,
   something truncated it.

   Create the file **before** the first `up`: a bind-mount whose host path does not
   exist becomes an empty *directory*, and the scraper now names that case
   explicitly. The same path also works as a Docker `secrets:` target.
2. **Double every `$`** in the compose file — the same example written as
   `MyP@ss$$w0rd` reaches the container correctly:
   ```yaml
   environment:
     - USMS_PASSWORD=MyP@ss$$w0rd
   ```
3. A password without `$` in it.

Quotes are a second trap: in the `- KEY=value` list form the quotes are **literal**
characters, so `- USMS_PASSWORD="MyP@ss$w0rd"` sends the quote characters too. The
scraper now strips one matching pair of surrounding quotes and logs a warning, and
leading/trailing spaces are stripped too. If a password contains `$` and no
`USMS_PASSWORD_FILE` is set, startup logs a warning explaining the truncation.

---


## MQTT Topics Published

- `home/usms/remaining_unit` — Remaining units on your smart meter  
- `home/usms/remaining_balance` — Remaining balance in your account  
- `home/usms/meter_last_polled` — Last meter polling timestamp  
- `home/usms/meter_no` — Meter number of the meter the topics above describe  
- `home/usms/last_run` — Timestamp of the last **fully successful** scrape cycle  
- `home/usms/status` — `ok` (every field of every meter), `partial` (some), `fail` (none), `offline` (last will)

### Accounts with more than one meter

A USMS account can hold several meters — typically electricity (`kWh`) and water
(`m³`). Every meter card on the dashboard is read, and when there is more than
one each gets its own topic branch named after the meter type:

- `home/usms/electricity/{remaining_unit,remaining_balance,meter_last_polled,meter_no,status}`
- `home/usms/water/{remaining_unit,remaining_balance,meter_last_polled,meter_no,status}`

The unmetered `home/usms/…` topics stay bound to the **electricity** meter (or the
first card if there is no electricity meter), so an existing Home Assistant setup
keeps working without changes. Two meters of the same type get the meter number
appended instead — `home/usms/electricity_55000001/…`. `status` at the top level is
`ok` only when every meter came back complete.

### Home Assistant entities (MQTT discovery)

For any account that needs the new branches — more than one meter, or a single
meter that is not electricity — the scraper publishes retained **MQTT discovery**
configs, so Home Assistant creates the entities itself and no YAML is needed:

- per meter: `sensor.usms_<meter>_remaining_unit`, `_remaining_balance`,
  `_meter_last_polled`, plus diagnostic `_meter_no` and `_status`
- `<meter>` is `water` / `electricity`, or `electricity_<meter_no>` when two meters
  share a type
- all sit under one HA device per meter, prefixed `USMS`

Discovery is deliberately **not** published for a single electricity meter: those
installations already have hand-written sensors on the legacy topics, and discovery
would add a duplicate set next to them. Requires `MQTT_BROKER` (discovery travels on
the same connection) and HA's MQTT integration.

All topics are published with `retain=True, qos=1`, so a subscriber that starts late still
receives the last values. A field that fails validation is simply not published — the
retained value from the previous good cycle stays put.

---

## Usage

### Prerequisites

- A USMS account registered to a smart meter (electricity `kWh`, water `m³`, or both)
- Docker Compose
- Optional: an MQTT broker, for publishing. Without one every reading is still
  printed to the container log — nothing else is required.

### Running with Docker Compose

1. Edit your `docker-compose.yml` to include:

```yaml
services:
  usms-scrape:
    image: famalama/usms-scrape:stable   # stable = current release; see "Installing and updating"
    restart: unless-stopped          # survive a host reboot and the built-in auto-heal exit
    container_name: usms-scrape
    environment:
      - USMS_USERNAME=01234567 #CHANGE (your IC number, as used on the USMS portal)
      # A password with NO dollar sign can live right here. If it contains `$`,
      # Compose will truncate it before the container starts — double the `$`
      # ($$) or, better, use USMS_PASSWORD_FILE from the section above.
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

```bash
docker compose logs -f usms-scrape
```

A healthy cycle looks like this, one line per meter plus one summary line:

```
config: interval=1800s selenium=selenium:4444 mqtt=on (192.168.1.10:1883)
        credentials username=set password=*** (11 chars) ...
cycle=2e6c begin (interval=1800s, selenium=selenium:4444, mqtt=on)
cycle=2e6c stage 1 accepted; solving the stage-2 captcha on .../ResLogin?sc=...
cycle=2e6c captcha attempt 2/10: 'baq4v' (5/5 models agree)
cycle=2e6c login successful — dashboard loaded at .../SmartMeter/MainPage
cycle=2e6c meter=55000001 kind=electricity unit=1976.141 kWh | balance=$67.01 | last polled=25/11/2026
cycle=2e6c mqtt home/usms/electricity/remaining_unit = '1976.141 kWh' (qos=1 retain, acked in 0ms)
cycle=2e6c result=ok meters=1 unit='1976.141 kWh' balance='$67.01' polled='25/11/2026' ...
cycle=2e6c sleeping 1785s (interval 1800s)
```

**Read the first line first.** `password=*** (11 chars)` is the fastest sanity
check there is: if that count is shorter than the password you set, something
truncated it (see "Passwords containing `$`, quotes or spaces").

A captcha the OCR cannot read confidently is re-rolled and logged as
`captcha attempt N/10: '<code>' (n/5 models agree)`; a disagreement costs a fresh
code, never a login attempt. If all rolls fail, the cycle is skipped and the last
good sensor values are left in place.

The scraper then runs unattended: one cycle every `SCRAPE_INTERVAL` seconds,
publishing to MQTT when configured.

---

## Installing and updating

| Tag | Meaning |
|-----|---------|
| `stable` | The current released version — run this |
| `v0.15.12`, `v0.15.11`, … | Immutable per-release tags, for pinning |
| `latest` | Also published and rebuilt with each release; `stable` is the one to prefer |

```bash
# upgrade
docker compose pull && docker compose up -d --force-recreate usms-scrape
```

Two things that catch people out:

- `docker compose restart` does **not** fetch a new image, and does **not** apply a
  changed environment variable or volume — that needs `up -d --force-recreate`.
- A change to `USMS_PASSWORD`, `USMS_USERNAME` or a `*_FILE` path is a recreate, not a
  restart.

To make upgrades deliberate, pin the version: `image: famalama/usms-scrape:v0.15.12`.

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

## Troubleshooting

Almost every failure is decided before the first browser request, so start with the
two startup lines: `config: … password=*** (N chars)` and either
`USMS_PASSWORD loaded from …` or a list of `config:` problems.

| What you see | What it means | Fix |
|---|---|---|
| The portal page in the log shows **`Incorrect user name and password`** | The container is sending a different password than the portal expects. Compose truncation is the usual cause, a changed password the second. | Compare `(N chars)` with the real password length. `$` present → `$$` or `USMS_PASSWORD_FILE`. |
| `CRITICAL config: USMS_PASSWORD is empty` then `Refusing to start` | The value arrived empty. A password that **starts** with `$` is emptied entirely by Compose. | Same fix. The process exits with code 2, so `restart: unless-stopped` shows a restart loop — that loop is the symptom. |
| `USMS_PASSWORD_FILE=… still contains the template placeholder` | `make-credentials.sh` ran but the file was never edited. | Replace the `PASTE-YOUR-PASSWORD-HERE` line. |
| `USMS_PASSWORD_FILE=… is a DIRECTORY` | The bind-mount host path did not exist when the stack first started, so Docker created a directory there. | `rmdir` it, create the file, recreate the container. |
| `USMS_PASSWORD is wrapped in " quotes — those are literal …` | Not an error: quotes inside a compose list entry are literal, so they were stripped. | Tidy the compose file when convenient. |
| The `selenium` container never turns healthy and the scraper never starts | Wrong healthcheck URL — Selenium 4.35+ answers 404 on `/wd/hub`. | Use `http://localhost:4444/wd/hub/status`. |
| `unit = '62.320 m³' does not look like a kWh reading` | An image **older than v0.15.12** on a water meter: only kWh was accepted, so the unit sensor never populated. | Update to v0.15.12 or newer. |
| `unit = '…' does not look like a meter reading with a unit` (v0.15.12+) | The cell held something other than a number with kWh/m³/L — a portal layout change or a placeholder such as `N/A`. | Check the saved page; other fields usually still publish. |
| `Incomplete scrape (meter … missing unit,balance,polled)` with a page title containing `Session Has Expired` | That cycle's login failed and the scrape ran anyway. | Scroll up: the login failure is the cause, the missing fields are the symptom. Older images printed `All values N/A` here instead. |
| `no meter card found by label — falling back to the fixed absolute XPaths`, then `fail` | The portal changed its dashboard markup. | The full page was saved inside the container at `/tmp/usms-fail-*.html`; open an issue with it. |
| `gave up reading the captcha after 10 attempts` | The OCR models could not agree on any of 10 codes. | Rare, usually follows a portal-side captcha change. Keep `SCRAPE_INTERVAL` at 300 or more while investigating. |
| `3 consecutive failed cycles — exiting so the restart policy … recycles us` | Deliberate auto-heal, not a crash. | Fix the underlying failure above; the exit is the messenger. |

When asking for help, include the `config:` line and the `result=` line — **not** a
full page dump: those contain the account holder's name and address (digits are
masked, the names are not).

---

## Home Assistant sensors

Two ways to get entities, pick one:

- **MQTT discovery (nothing to write)** — for a multi-meter, water-only or gas
  account the scraper publishes discovery configs and HA builds the sensors itself
  on the first cycle (see "Home Assistant entities (MQTT discovery)" above). If you
  see `sensor.usms_water_remaining_unit`, this is already working and you can skip
  the YAML below.
- **`configuration.yaml` (below)** — the classic route, bound to the legacy
  `home/usms/*` topics, which follow the **electricity** meter.

> **A water reading does not fit this YAML.** `unit_of_measurement: kWh` and the
> balance's `BND` are electricity assumptions. For a water meter either let
> discovery create the entities, or copy these four sensors onto the
> `home/usms/water/…` topics with `unit_of_measurement: m³`.

Insert this into your `configuration.yaml`:

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

| File | What it is |
|---|---|
| `usms.py` | The whole scraper: config, login, captcha, meter reading, MQTT, supervisor loop |
| `Dockerfile` | `python:3.12-slim` + selenium + paho-mqtt + ddddocr |
| `make-credentials.sh` | Writes a commented `usms_pw` for the `*_FILE` route |
| `.github/workflows/` | Builds and pushes the image on a `v*` tag |

Behaviour worth knowing before changing anything:

- **Login is two stages.** `ResLogin` takes IC + password, then hands over to
  `ResLogin?sc=…`, which asks for both again **plus a captcha**. The code is read by
  `ddddocr` (CPU-only ONNX, no network call, no human), and the captcha image is read
  out of Chrome's own HTTP cache because its URL is single-use.
- **Meter cards are found by label text** (`//td[normalize-space(.)='Remaining Unit:']`),
  not by row index. `normalize-space(text())` matches nothing here: the caption lives
  in a `<span>` inside the cell. The old absolute XPaths remain only as a fallback.
- **Everything is validated before it is published.** A field that fails its shape
  check is skipped, so a shifted table row never overwrites a good sensor.
- **Failures count.** Three consecutive failed cycles exit the process so
  `restart: unless-stopped` recycles it with a clean Selenium session.

Testing without touching the portal: save the dashboard once
(`driver.page_source`), `docker cp` it into a throwaway `selenium/standalone-chrome`
container, then run the module's own functions against `file:///tmp/mainpage.html`
and mutate the DOM in JS for edge cases. Publishing can be proven against a
throwaway `eclipse-mosquitto:2` on the same docker network with
`mosquitto_sub -t 'homeassistant/#' -t 'home/usms/#' -v`.

### Never commit credentials

A real account password once reached this repository and, although the branch was
force-pushed clean, GitHub still served the old object by SHA — the password had to
be rotated. Do not put credentials in code, tests, fixtures, commit messages or a
saved page dump: page dumps contain the account holder's name and address, and
digits are masked in logs only.


## Change history

### v0.15.12
- **Multi-meter accounts** — every meter card on the dashboard is read. Electricity
  and water each get their own `home/usms/<kind>/…` topics; the legacy topics follow
  the electricity meter, so existing setups are unaffected.
- **Water/volume readings** (`m³`, `m3`, `L`) pass validation. They used to be
  discarded as "not a kWh reading", which left the unit sensor permanently unknown.
- **Credentials that survive Compose** — `USMS_USERNAME_FILE` / `USMS_PASSWORD_FILE`,
  a `make-credentials.sh` generator with an in-file comment header, quote stripping,
  a warning when the password contains `$`, and the password length in the startup
  line so a truncation is visible.
- **MQTT discovery** for multi-meter and water-only accounts: HA creates the sensors
  and a device per meter itself. Deliberately skipped for a single electricity meter,
  whose installation usually already has hand-written sensors.
- Meter discovery by label text, with the old absolute XPaths kept as a fallback.

### v0.15.11
- More INFO/DEBUG detail, proven under fault injection: per-navigation timings, every
  captcha reading with its inference time, and *why* a roll was rejected.

### v0.15.10
- The documented failure handling made real: only validated fields are published,
  `last_run` advances only on a full cycle, and 3 consecutive failed cycles exit.

### v0.15.9
- Two-stage login with the stage-2 captcha solved by local OCR.

## License

This project is licensed under the GNU General Public License v3.0 (GPLv3). See LICENSE for details.
