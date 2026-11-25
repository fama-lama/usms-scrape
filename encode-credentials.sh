#!/usr/bin/env bash
# Put a password into docker-compose.yml in a form Compose cannot mangle.
#
#   ./encode-credentials.sh                     # prints the lines to paste
#   ./encode-credentials.sh --compose compose.yaml
#                                               # rewrites the file itself (backup kept)
#
# WHY THIS EXISTS — Docker Compose INTERPOLATES `$` in every value it reads
# (environment:, env_file:, .env). A password like `$ecret1` arrives EMPTY and
# `MyP@ss$w0rd` arrives as `MyP@ss`, before the container ever starts, and the portal
# then answers "Incorrect user name and password" — the same message as a genuinely
# wrong password. Nothing in the image can recover those characters: they are gone
# before the process exists. Base64 (A–Z a–z 0–9 + / =) contains no `$`, so an encoded
# value passes through untouched, and the scraper decodes it at startup.
#
# With --compose there is no copy-paste step: the script replaces the USMS_PASSWORD
# line (and USMS_USERNAME, if you give one) in place, writes a timestamped backup
# beside it, and reports what changed.
set -euo pipefail

b64() { printf '%s' "$1" | base64 | tr -d '\n'; }

ask() {
  local prompt="$1" value
  printf '%s' "$prompt" >&2
  IFS= read -r -s value
  printf '\n' >&2
  printf '%s' "$value"
}

compose=""
while [ $# -gt 0 ]; do
  case "$1" in
    --compose) compose="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

username="$(ask 'USMS username / IC number or email (blank to keep as-is): ')"
password="$(ask 'USMS password (blank to cancel): ')"

if [ -z "$password" ] && [ -z "$username" ]; then
  echo "nothing entered — no change made" >&2
  exit 1
fi

enc_password=""
[ -n "$password" ] && enc_password="b64:$(b64 "$password")"

# ---------------------------------------------------------------- print mode
if [ -z "$compose" ]; then
  echo "# ── paste these into the usms-scrape service's environment: ──"
  echo
  [ -n "$username" ] && echo "      - USMS_USERNAME=$username"
  if [ -n "$enc_password" ]; then
    echo "      - USMS_PASSWORD=$enc_password"
    echo "        # ${#password} characters, dollar signs included"
  fi
  echo
  echo "# ── one form only: delete any other USMS_PASSWORD / *_B64 / *_FILE line ──"
  exit 0
fi

# ------------------------------------------------------- in-place rewrite mode
[ -f "$compose" ] || { echo "ERROR: $compose not found" >&2; exit 1; }

backup="$compose.bak-cred-$(date +%Y%m%d-%H%M%S)"
cp -p "$compose" "$backup"

python3 - "$compose" "$username" "$enc_password" "$backup" <<'PY'
import re
import sys

path, username, enc_password, backup = sys.argv[1:5]
text = open(path, encoding="utf-8").read()
original = text
changed = []


def replace_value(text, var, value):
    pattern = re.compile(r"^(\s*-\s*%s\s*=)(.*)$" % var, re.M)
    if pattern.search(text):
        return pattern.sub(lambda m: m.group(1) + value, text), "list"
    pattern = re.compile(r"^(\s*%s\s*:\s*)(.*)$" % var, re.M)
    if pattern.search(text):
        return pattern.sub(lambda m: m.group(1) + value, text), "map"
    return text, None


for var, value in (("USMS_USERNAME", username), ("USMS_PASSWORD", enc_password)):
    if not value:
        continue
    text, form = replace_value(text, var, value)
    changed.append("%s (%s form)" % (var, form) if form
                   else "%s — NOT FOUND, add it manually" % var)

removed = []
for var in ("USMS_PASSWORD_B64", "USMS_USERNAME_B64",
            "USMS_PASSWORD_FILE", "USMS_USERNAME_FILE"):
    new = re.sub(r"^\s*-\s*%s=.*\r?\n" % var, "", text, flags=re.M)
    new = re.sub(r"^\s*%s\s*:.*\r?\n" % var, "", new, flags=re.M)
    if new != text:
        removed.append(var)
        text = new

if text == original:
    print("no change written")
    sys.exit(1)

open(path, "w", encoding="utf-8").write(text)
print("rewrote %s" % path)
for line in changed:
    print("  set %s" % line)
for line in removed:
    print("  removed competing %s line" % line)
print("  backup: %s" % backup)
PY

echo
echo "NEXT: docker compose up -d --force-recreate usms-scrape"
echo "The first log line should read 'password=*** (N chars, b64)' with the right N,"
echo "and 'login successful' should follow."
echo "To undo:  mv '$backup' '$compose'"
