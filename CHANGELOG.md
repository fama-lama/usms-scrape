# Changelog

All notable changes to the USMS scraper. Versions are tagged `vX.Y.Z` and published to
Docker Hub as `vX.Y.Z`, `stable` and `latest`.

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
