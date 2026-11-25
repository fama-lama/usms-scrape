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
- **Credential handling**: one variable per credential. `USMS_PASSWORD` accepts a **plain or a base64 password** — base64 is detected automatically, and it is the way to survive a `$` (Docker Compose interpolates `$` before the container starts; quotes do not help). Also: quote stripping, the resolved length and form in the startup line, and a credential check that parks instead of burning the portal's login attempts.
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
| `USMS_USERNAME`  | Account username — the IC number, digits only (no dashes or spaces)             | Yes | N/A |
| `USMS_PASSWORD`  | Account password — **plain or base64 in the same variable**, worked out automatically; plain breaks if it contains `$` | Yes | N/A |
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

### Credentials

Two variables. Nothing else to set — the password accepts plain or base64 in the **same**
variable and the scraper works out which it got.

```yaml
environment:
  - USMS_USERNAME=01234567           # the IC number, digits only
  - USMS_PASSWORD=YOUR-PASSWORD-HERE
```

| paste this | when |
|---|---|
| `MyPassword123` | the password has no `$` — the simple case |
| `PASTE-BASE64-HERE` | the password has `$` — encode it once, below |
| `b64:PASTE-BASE64-HERE` | optional explicit spelling of the base64 form |
| `plain:whatever` | rare: the password genuinely looks like base64 |

#### If the password contains `$`

Docker Compose substitutes `$...` **before the container starts**, so the password arrives
truncated (or empty) and the portal answers "Incorrect user name and password" — the same
message as a wrong password. Quotation marks do not help. Encode it once:

1. Open <https://emn178.github.io/online-tools/base64_encode.html> — open source
   ([source](https://github.com/emn178/online-tools)), and the conversion happens in your
   browser. Keep the format on **RFC 4648**; do not choose "URL Safe".
2. Paste the password, copy the **Output**.
3. Paste it into `USMS_PASSWORD` (the `b64:` prefix is optional).
4. `docker compose up -d --force-recreate usms-scrape`
5. `docker compose logs usms-scrape | head -3` — the first line must read
   `password=*** (11 chars, b64)` with the real length. A shorter number means only part
   of the Output was copied.

Only the password needs encoding. The username is the IC number, typed straight in.

**Why it cannot fix this for you:** Compose deletes those characters before the process
exists, so no image can reconstruct them. Everything after that point is automatic.

Worth knowing:

- **Quoting a `$` does not protect it.** Measured, four ways, all truncated:

  | written as | the container receives |
  |---|---|
  | `- USMS_PASSWORD=MyP@ss$w0rd` | `MyP@ss` |
  | `- USMS_PASSWORD='MyP@ss$w0rd'` | `'MyP@ss'` (the quotes become part of the value) |
  | `- USMS_PASSWORD="MyP@ss$w0rd"` | `"MyP@ss"` |
  | `USMS_PASSWORD: 'MyP@ss$w0rd'` | `MyP@ss` |

- **The username is the IC number**: digits only, no dashes or spaces. A typo is flagged
  at startup rather than sent to the portal.
- **Base64 is encoding, not encryption** — anyone who can read the compose file can decode
  it. If that is not acceptable, keep the value in a secrets manager and inject it at
  deploy time.

---

### The scraper checks your credentials

The first thing every cycle does is log in, and the log says exactly what happened:

```
config: ... credentials username=set (ic) password=*** (11 chars, plain) ...
cycle=2e6c stage 1 accepted; solving the stage-2 captcha ...
cycle=2e6c login successful — dashboard loaded at .../MainPage
```

If the **portal itself** rejects the username/password, that is treated as a
configuration problem, not a hiccup — and the scraper **stops attempting logins**,
because the portal allows only a handful of tries before it locks the account:

```
CRITICAL cycle=5203 the USMS portal REJECTED the credentials
         (stage 1: the portal answered 'Incorrect user name and password').
CRITICAL cycle=5203   username: '01234599' (8 chars, from plain, looks like ic)
CRITICAL cycle=5203   password: 16 chars, from plain
CRITICAL cycle=5203   If the password you typed is longer than 16 characters,
         Docker Compose truncated it at a '$'. Fix it by putting the value in the
         same variable as b64:<base64> ...
CRITICAL cycle=5203   If the length matches what you typed, the portal password
         itself is different from this one: log in to https://www.usms.com.bn ...
CRITICAL Waiting for the credentials to be corrected. Change USMS_USERNAME /
         USMS_PASSWORD ... and recreate the container
```

The container stays **up** (so `docker ps` shows it and the reason is in the log) but
it makes no further login attempts. Fix the compose file and
`docker compose up -d --force-recreate usms-scrape` to retry.

Two things this deliberately does *not* do:

- It does not count a rejected credential toward the "3 failed cycles then exit"
  auto-heal — a restart would just burn another attempt.
- It does not guess that a value was truncated. It cannot: Compose deletes the
  characters before the process starts. What it gives you is the length, the source
  and the exact fix.

## MQTT Topics Published

- `home/usms/remaining_unit` — Remaining units on your smart meter  
- `home/usms/remaining_balance` — Remaining balance in your account  
- `home/usms/meter_last_polled` — Last meter polling timestamp  
- `home/usms/meter_no` — Meter number of the meter the topics above describe  
- `home/usms/last_run` — Timestamp of the last **fully successful** scrape cycle  
- `home/usms/status` — `ok` (every field of every meter), `partial` (some), `fail` (none), `offline` (last will)

### Per-meter topics — published for EVERY account

Every meter card on the dashboard is read, and every meter always gets its own
topic branch named after its type:

- `home/usms/electricity/{remaining_unit,remaining_balance,meter_last_polled,meter_no,status}`
- `home/usms/water/{remaining_unit,remaining_balance,meter_last_polled,meter_no,status}`

This happens whether the account holds one meter or three — a branch that appeared
only for some accounts would be a trap.

The **legacy** `home/usms/*` topics are an alias for ONE meter: the electricity one,
or the first card when there is no electricity meter. That is what existing
dashboards and the sensor YAML below use, so nothing breaks on upgrade.

Two meters of the same type get the meter number appended instead —
`home/usms/electricity_55000001/…`. Top-level `status` is `ok` only when **every**
meter came back complete.

### Home Assistant entities (MQTT discovery)

For every account **except the single-electricity-meter case**, the scraper
publishes retained **MQTT discovery** configs, so Home Assistant creates the entities
itself and no YAML is needed:

- per meter: `sensor.usms_<meter>_remaining_unit`, `_remaining_balance`,
  `_meter_last_polled`, plus diagnostic `_meter_no` and `_status`
- `<meter>` is `water` / `electricity`, or `electricity_<meter_no>` when two meters
  share a type
- all sit under one HA device per meter, prefixed `USMS`

Discovery is deliberately **not** published for a single electricity meter: that is
the case where hand-written sensors on the legacy topics usually already exist, and
discovery would add a duplicate set next to them. Everything else — a water meter, a
gas meter, or any account with two or more meters — gets the entities automatically.
Requires `MQTT_BROKER` (discovery travels on the same connection) and HA's MQTT
integration.

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
    image: famalama/usms-scrape:latest   # latest = current release; see "Installing and updating"
    restart: unless-stopped          # survive a host reboot and the built-in auto-heal exit
    container_name: usms-scrape
    environment:
      - USMS_USERNAME=01234567 #CHANGE (your IC number, as used on the USMS portal)
      # A password with no `$` can live right here. If it contains `$`, Compose
      # truncates it before the container starts — use the base64 form instead
      # (see "Credentials" above):
      #   - USMS_PASSWORD=<paste the base64 output here>
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
        credentials username=set password=*** (11 chars, plain) ...
cycle=2e6c begin (interval=1800s, selenium=selenium:4444, mqtt=on)
cycle=2e6c stage 1 accepted; solving the stage-2 captcha on .../ResLogin?sc=...
cycle=2e6c captcha attempt 2/10: 'baq4v' (5/5 models agree)
cycle=2e6c login successful — dashboard loaded at .../SmartMeter/MainPage
cycle=2e6c meter=55000001 kind=electricity unit=1976.141 kWh | balance=$67.01 | last polled=25/11/2026
cycle=2e6c mqtt home/usms/electricity/remaining_unit = '1976.141 kWh' (qos=1 retain, acked in 0ms)
cycle=2e6c result=ok meters=1 unit='1976.141 kWh' balance='$67.01' polled='25/11/2026' ...
cycle=2e6c sleeping 1785s (interval 1800s)
```

**Read the first line first.** `password=*** (11 chars, plain)` is the fastest sanity
check there is: if that count is shorter than the password you set, something
truncated it (see "If the password contains `$`").

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
| `latest` | The current release — run this |
| `stable` | The same image, published under both names |
| `v0.15.13`, `v0.15.12`, … | Immutable per-release tags, for pinning |

```bash
# upgrade
docker compose pull && docker compose up -d --force-recreate usms-scrape
```

Two things that catch people out:

- `docker compose restart` does **not** fetch a new image, and does **not** apply a
  changed environment variable or volume — that needs `up -d --force-recreate`.
- A change to `USMS_PASSWORD` or `USMS_USERNAME` is a recreate, not a
  restart.

To make upgrades deliberate, pin the version: `image: famalama/usms-scrape:v0.15.13`.

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
- The live view shows the browser, not the OCR: captcha reads are logged as `captcha attempt N/10: '<code>' (n/5 models agree)`.

## Troubleshooting

Almost every failure is decided before the first browser request, so start with the
startup lines: `config: … credentials username=set (ic) password=*** (N chars, plain|b64)`,
any `WARNING USMS_PASSWORD contains '$'`, and — when the container refuses to start — the
`CRITICAL config: …` lines that say what is wrong.

| What you see | What it means | Fix |
|---|---|---|
| The portal page in the log shows **`Incorrect user name and password`** | The container is sending a different password than the portal expects. Compose truncation is the usual cause, a changed password the second. | Compare `(N chars)` with the real password length. `$` present → paste the base64 instead. |
| `the USMS portal REJECTED the credentials` then `Waiting for the credentials to be corrected` | The username/password the container holds are wrong. The scraper parks instead of retrying, to avoid locking the account. | Read the printed lengths, fix the value (base64 form survives `$`), then `up -d --force-recreate`. |
| `the credential looks like base64 and was decoded automatically` | Not an error unless the password is literally base64-shaped. | Prefix the value with `plain:` to keep it literal. |
| `USMS_USERNAME=… is neither a plain IC number (digits) nor an email address` | The IC has dashes, spaces or a typo. | Enter the IC as digits only, e.g. `01234567`. |
| `CRITICAL config: USMS_PASSWORD is empty — set it in the stack's environment` then `Refusing to start with an unusable configuration.` | The value arrived empty. A password that **starts** with `$` is emptied entirely by Compose. | Same fix. The process exits with code 2, so `restart: unless-stopped` shows a restart loop — that loop is the symptom. |
| `USMS_PASSWORD could not be decoded …` | A `b64:`/base64 value was truncated, or non-base64 was pasted. | Encode the password again (see "If the password contains `$`") and paste the whole Output. |
| `password=*** (N chars, b64 (auto-detected))` | Not an error: the value was recognised as base64 and decoded. | Nothing — unless the password genuinely *is* that string, then write `plain:<value>`. |
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

There are two ways to get entities into Home Assistant. **Check the first one before
writing any YAML** — for a water, gas or multi-meter account it has probably already
created what you need.

### 1. MQTT discovery (automatic, no YAML)

On the first cycle the scraper publishes retained discovery configs and HA builds
the entities itself. Look for these under **Settings → Devices & Services → MQTT**:

| Meter type | Entities created |
|---|---|
| water | `sensor.usms_water_remaining_unit`, `sensor.usms_water_remaining_balance`, `sensor.usms_water_meter_last_polled`, `sensor.usms_water_meter_no` *(diagnostic)*, `sensor.usms_water_status` *(diagnostic)* |
| electricity (multi-meter accounts) | the same five with `usms_electricity_…` |
| two meters of one type | `usms_electricity_<meter_no>_…` |

Each meter becomes its own HA device (`USMS Water meter`, `USMS Electricity meter`)
with the meter number as its serial number.

Discovery is deliberately **skipped for a single electricity meter**, because that is
the case where hand-written YAML sensors (below) usually already exist and discovery
would create a duplicate set beside them. Everything else gets it automatically —
including a water-only account and any account with more than one meter.

Requires `MQTT_BROKER` (discovery rides the same connection) and HA's MQTT
integration. If the entities do not appear, check the log line
`published N discovery config(s) for M meter(s)`.

### 2. YAML sensors

Pick the block that matches your account.

**Electricity — the legacy topics** (these exist for every account; they follow the
electricity meter). This is the historical block, unchanged:

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

**Water — the per-meter topics.** Same shape, different topic branch and unit:

```yaml
mqtt:
  sensor:
    # "62.320 m³" -> 62.320
    - name: USMS Water Remaining Unit
      state_topic: home/usms/water/remaining_unit
      value_template: "{{ value.split(' ')[0] }}"
      unit_of_measurement: "m³"
      state_class: total

    - name: USMS Water Remaining Balance
      state_topic: home/usms/water/remaining_balance
      value_template: "{{ value.replace('$', '') }}"
      unit_of_measurement: "BND"
      state_class: total

    - name: USMS Water Meter Last Polled
      state_topic: home/usms/water/meter_last_polled
      value_template: "{{ value }}"

    # Health of the water scrape on its own (ok / partial / fail)
    - name: USMS Water Status
      state_topic: home/usms/water/status
      value_template: "{{ value }}"
```

**Electricity on a multi-meter account** uses the same shape on
`home/usms/electricity/…` — worth doing if you want a per-meter `status`, because the
legacy `home/usms/status` reports the whole cycle, not one meter.

> **Do not** put `unit_of_measurement: kWh` on a water topic (or `m³` on an
> electricity one). HA will happily accept the mismatch and your energy statistics
> become nonsense.

## Development

| File | What it is |
|---|---|
| `usms.py` | The whole scraper: config, login, captcha, meter reading, MQTT, supervisor loop |
| `Dockerfile` | `python:3.12-slim` + selenium + paho-mqtt + ddddocr |
| `CHANGELOG.md` | Released changes, newest first |
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

### v0.15.13

- Credentials are one variable each: `USMS_USERNAME` and `USMS_PASSWORD`. The
  `*_FILE` and `*_B64` variables are gone — `USMS_PASSWORD` accepts plain, `b64:`,
  bare base64 and `plain:` in the same place.
- `make-credentials.sh` removed and `encode-credentials.sh` dropped: encoding is a
  one-off step done on any base64 tool (link in the credentials section), not a file to
  keep in the repository

### v0.15.12
- **Multi-meter accounts** — every meter card on the dashboard is read. Every meter
  gets its own `home/usms/<kind>/…` topic branch (always, even for a single meter);
  the legacy `home/usms/*` topics alias the electricity meter, so existing setups are
  unaffected.
- **Water/volume readings** (`m³`, `m3`, `L`) pass validation. They used to be
  discarded as "not a kWh reading", which left the unit sensor permanently unknown.
- **Credentials that survive Compose, and are checked** — `b64:<base64>` inline in the
  same compose variable, quote stripping, a warning when the password
  contains `$`, the password length and source in the startup line, a username shape check
  (IC digits only), and — when the portal rejects the credentials — a full diagnosis plus a
  stop-all-attempts park, so a wrong password can no longer lock the account. The variable
  set was reduced to one per credential in v0.15.13.
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
