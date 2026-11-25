# Changelog

All notable changes to the USMS scraper. Versions are tagged `vX.Y.Z` and published to
Docker Hub as `vX.Y.Z`, `stable` and `latest`.

## v0.15.16

### Fixed
- **No readings on an account whose dashboard renders the meter table inside an
  `<iframe>`.** The reader only ever looked at the top document, so it waited for a
  meter card that was never going to appear there, fell back to the old single-meter
  paths, found nothing, and published a failing placeholder device. It now walks the
  top document and then each frame, and an incomplete-read retry re-enters the frame
  that worked.
- **A cycle that found no meter said nothing useful.** It now logs the page title,
  URL and masked body, and keeps the HTML so the next report can be diagnosed instead
  of guessed at.
- **The placeholder device stayed in Home Assistant forever.** Once a real meter is
  readable its retained topics and discovery configs are cleared, so the dead entity
  disappears.

## v0.15.15

### Fixed
- **MQTT publishes could be dropped while the connection looked fine.** `connect()` only
  starts the handshake; until the broker's CONNACK arrives, every `publish()` returns
  `MQTT_ERR_NO_CONN` ("The client is not currently connected.") and the message is
  discarded. On a fast local broker the CONNACK wins the race, which is why this looked
  healthy in testing — at a site with a slower broker no reading and no discovery config
  ever arrived, while the cycle logged every topic as `false`. The client now waits for
  the CONNACK before publishing.
- A refused connection is reported with the broker's own reason — e.g.
  `the broker refused the connection: Not authorized — check MQTT_USERNAME / MQTT_PASSWORD`
  — instead of an apparently successful cycle.
- MQTT v3.1.1 is used automatically when a broker refuses MQTT v5.

## v0.15.15

### Fixed
- **MQTT publishes were dropped while the connection looked healthy.** `connect()` only
  starts the handshake; until the broker's CONNACK arrives, every `publish()` returns
  `MQTT_ERR_NO_CONN` and the message is discarded. On a fast local broker the CONNACK wins
  that race, which is why it looked fine in testing — elsewhere no reading and no discovery
  config ever arrived while the cycle logged every topic as `false`. The client now waits
  for the CONNACK, and a refusal is reported with the broker's own reason.
- **A broker or Home Assistant outage exited the container every three cycles**, restarting
  it and re-authenticating against the portal for something a restart cannot heal. Only the
  scrape status drives that now; a publish failure is logged and ignored.
- **No HTTP timeout on the WebDriver client**: one wedged chromedriver could block the cycle
  forever with no log line, no failure counter and no restart.
- **A meter that disappeared left ghost entities**: its retained topics and discovery
  configs kept showing the last reading in Home Assistant forever. They are now cleared.
- **Two meters could share one topic branch** when their numbers were equal or differed only
  in punctuation — the later card overwrote the earlier and Home Assistant refused the
  duplicate `unique_id`. Slugs are now de-duplicated.
- **Negative and parenthesised balances** (arrears) were discarded as unreadable, freezing
  the sensor exactly when the account was in debit.
- **`Wh` readings** validated but were labelled `meter` instead of `electricity`.
- **A quoted `"b64:…"` password never decoded** — quotes were stripped after the decode.
- **Short passwords that merely looked like base64 were silently decoded** into a different
  secret; auto-detection now requires 16 characters.
- **Rate-limited / rejected credentials at the second login stage** were retried as a
  transient failure, burning the portal's attempt budget. They now park like stage 1.
- **Parking published nothing**, so Home Assistant kept showing `ok` while the scraper was
  definitively broken. It now publishes `fail` once on the way into the park.
- **Page dumps accumulated** in `/tmp` until Chrome could no longer start; at most 5 are kept.
- **A renamed login field raised a bare traceback** with no page dump; it now dumps and
  fails the cycle cleanly.
- **A missing captcha image re-rolled ten times** against the portal; it now stops.
- **The incomplete-scrape retry merged values by list position**, so a recovered value could
  come from a different meter. It pairs by meter number or not at all.
- **`read_meters` read the cards before they finished rendering**, which could silently
  report one meter of two as a complete scrape.
- **Uneven card labels were paired by index**, publishing one meter's reading under another
  meter's number. Each card is now read in its own scope.
- **A `$` in `MQTT_PASSWORD`** got none of the protection the portal password has.
- **A junk `MQTT_PORT`** aborted startup even when MQTT was disabled.

### Security
- The credentials-rejected report printed the account IC number in full while every other
  log line masks digits. It is masked now.

### Changed
- **Use the `b64:` prefix for an encoded password.** Auto-detection needs 16+ characters to
  stay safe, and a short password encodes to a shorter string (9 characters becomes 12);
  such a value is now used literally and says so in a warning instead of being guessed at.

## v0.15.14

### Changed
- Encoding a password containing `$` is a one-off step on any base64 tool; the workflow is
  documented with an open-source browser encoder. No helper script ships with the repo.

### Removed
- `encode-credentials.sh` — it saved one copy-paste and needed to be kept in step with the
  container's parser.

## v0.15.12

### Added
- Multi-meter accounts: every meter card on the dashboard is read, each published under
  its own `home/usms/<kind>/*` topics (`electricity`, `water`, or `<kind>_<meter>` when two
  meters share a type).
- MQTT discovery for multi-meter and water-only accounts, so Home Assistant creates the
  per-meter sensors and one device per meter without hand-written YAML.
- Base64 credentials: `b64:<base64>` inline in the usual variables (later versions dropped
  the extra `*_B64` / `*_FILE` spellings — one variable now accepts both forms).

### Changed
- Legacy `home/usms/*` topics follow the electricity meter; existing dashboards keep working.
- Startup line reports the credential source and the resolved password length.
- A credential the portal rejects is treated as a configuration problem: the cycle is
  diagnosed in the log and no further login attempts are made, so the account is not
  locked out.

### Fixed
- Water and volume readings (`m³`, `m3`, `L`) were discarded as "not a kWh reading".
- Meter cards are located by label text instead of fixed absolute XPaths, which only ever
  resolved the first card.
- A masked password length, quote stripping and a named error for a misconfigured
  credential file replace the previous silent login failures.

### Security
- Credential examples in the code and documentation use placeholders only.

## v0.15.11

### Added
- More INFO/DEBUG detail under fault injection: per-navigation timings, every captcha
  reading with its inference time, and the reason a roll was rejected.

## v0.15.10

### Fixed
- The documented failure handling is real: only validated fields are published,
  `last_run` advances only on a complete cycle, and three consecutive failed cycles exit
  so the restart policy recycles the container.

## v0.15.9

### Added
- Two-stage login with the stage-2 captcha solved locally by OCR.
