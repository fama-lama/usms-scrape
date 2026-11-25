#!/usr/bin/env bash
# Print the compose lines for a password that Docker Compose cannot mangle.
#
#   ./encode-credentials.sh
#
# Nothing is written to disk and no mount is needed: the output goes straight into
# the compose file, exactly like the old plain-text style.
#
# WHY THIS EXISTS — Docker Compose INTERPOLATES `$` in every value it reads
# (environment:, env_file:, .env). A password like `$ecret1` arrives EMPTY and
# `MyP@ss$w0rd` arrives as `MyP@ss`, before the container ever starts, and the
# portal then answers "Incorrect user name and password" — the same message as a
# genuinely wrong password.
#
# Base64 output uses only A–Z a–z 0–9 + / =, none of which Compose treats specially,
# so the encoded value survives untouched. The scraper decodes it at startup and
# logs the LENGTH it ended up with, never the value.
set -euo pipefail

b64() { printf '%s' "$1" | base64 | tr -d '\n'; }

ask_password() {
  local prompt="$1" value
  printf '%s' "$prompt" >&2
  IFS= read -r -s value
  printf '\n' >&2
  printf '%s' "$value"
}

echo "# ── paste these two lines into the usms-scrape service's environment: ──"
echo

username="$(ask_password 'USMS username / IC number (blank to skip): ')"
if [ -n "$username" ]; then
  echo "      - USMS_USERNAME_B64=$(b64 "$username")"
  echo "        # decodes to: $username"
fi

password="$(ask_password 'USMS password (blank to skip): ')"
if [ -n "$password" ]; then
  echo "      - USMS_PASSWORD_B64=$(b64 "$password")"
  echo "        # ${#password} characters, dollar signs included"
fi

echo
echo "# ────────────────────────────────────────────────────────────────────────"
echo "# No volume, no file. Remove any USMS_USERNAME / USMS_PASSWORD / *_FILE"
echo "# lines for the same value — one route only, the scraper warns if it sees"
echo "# more than one."
echo "#"
echo "# Verify after 'docker compose up -d --force-recreate usms-scrape':"
echo "#   config: ... credentials username=set (b64) password=set (N chars, b64) ..."
echo "# If N is shorter than the password you typed, something mangled it."
