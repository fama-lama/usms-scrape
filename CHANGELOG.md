# Changelog

All notable changes to the USMS scraper. Versions are tagged `vX.Y.Z` and published to
Docker Hub as `vX.Y.Z`, `stable` and `latest`.

## v0.15.12

### Added
- Multi-meter accounts: every meter card on the dashboard is read, each published under
  its own `home/usms/<kind>/*` topics (`electricity`, `water`, or `<kind>_<meter>` when two
  meters share a type).
- MQTT discovery for multi-meter and water-only accounts, so Home Assistant creates the
  per-meter sensors and one device per meter without hand-written YAML.
- Base64 credentials: `b64:<base64>` inline in the usual variables, the equivalent
  `USMS_USERNAME_B64` / `USMS_PASSWORD_B64`, and `USMS_*_FILE` for a mounted file.
- `encode-credentials.sh` and `make-credentials.sh` helpers.

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
