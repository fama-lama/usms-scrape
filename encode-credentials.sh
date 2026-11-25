#!/usr/bin/env bash
# Encode the USMS password so Docker Compose cannot mangle a '$'.
#
#   ./encode-credentials.sh                        # print the line to paste
#   ./encode-credentials.sh --compose compose.yaml # rewrite the file (backup kept)
#
# WHY: Compose interpolates `$` in every value it reads, so `MyP@ss$w0rd` arrives as
# `MyP@ss` before the container starts. Base64 has no `$` in its alphabet, so it passes
# through untouched and the scraper decodes it — in the same variable, no extra setting.
# Nothing in the image can recover characters Compose deleted before the process existed.
set -euo pipefail

compose=""
case "${1:-}" in
  --compose) compose="${2:?usage: $0 [--compose path]}" ;;
  "" ) ;;
  -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

printf 'USMS password (typing is not echoed): ' >&2
IFS= read -rs pw || true          # a piped value may arrive without a trailing newline
echo >&2
[ -n "$pw" ] || { echo "nothing entered — no change made" >&2; exit 1; }

if base64 --help 2>&1 | grep -q -- '-w'; then
  enc=$(printf '%s' "$pw" | base64 -w0)
else
  enc=$(printf '%s' "$pw" | base64 | tr -d '\n')
fi

if [ -z "$compose" ]; then
  cat <<EOF

Paste into the usms-scrape service's environment:

  - USMS_PASSWORD=${enc}

Then:  docker compose up -d --force-recreate usms-scrape
Check: docker compose logs usms-scrape | head -3    →    password=*** (${#pw} chars, b64)
EOF
  exit 0
fi

[ -f "$compose" ] || { echo "ERROR: $compose not found" >&2; exit 1; }
backup="$compose.bak-cred-$(date +%Y%m%d-%H%M%S)"
cp -p "$compose" "$backup"

python3 - "$compose" "$enc" <<'PY'
import re
import sys

path, enc = sys.argv[1:3]
text = open(path, encoding="utf-8").read()
before = text

text = re.sub(r"^(\s*-\s*USMS_PASSWORD\s*=).*$", lambda m: m.group(1) + enc, text, flags=re.M)
text = re.sub(r"^(\s*USMS_PASSWORD\s*:\s*).*$", lambda m: m.group(1) + enc, text, flags=re.M)

# drop the variables this version no longer reads, so an old line cannot confuse anyone
for var in ("USMS_PASSWORD_B64", "USMS_USERNAME_B64",
            "USMS_PASSWORD_FILE", "USMS_USERNAME_FILE"):
    text = re.sub(r"^\s*-?\s*%s[:=].*\r?\n" % var, "", text, flags=re.M)

if "USMS_PASSWORD" not in text:
    print("ERROR: no USMS_PASSWORD line in %s — add:\n  - USMS_PASSWORD=%s" % (path, enc))
    sys.exit(1)
if text == before:
    print("no change needed")
    sys.exit(1)

open(path, "w", encoding="utf-8").write(text)
print("rewrote %s" % path)
PY

echo "backup: $backup"
echo "NEXT: docker compose up -d --force-recreate usms-scrape"
echo "Undo: mv '$backup' '$compose'"
