#!/usr/bin/env bash
# Create the credentials file the scraper reads at /run/secrets/usms_pw.
#
#   ./make-credentials.sh [path]        (default: ./usms_pw)
#
# Why a file at all: Docker Compose INTERPOLATES `$` in every value it reads
# (environment:, env_file:, .env), so a password like `$ecret1` or `MyP@ss$w0rd`
# is truncated or emptied before the container ever starts. A file the scraper
# opens itself is never touched by Compose, so `$`, quotes, spaces and
# backslashes all survive.
#
# The file this writes carries a comment header; the scraper ignores lines that
# start with `#` and blank lines, and uses the first other line. Until you
# replace the PASTE-YOUR-PASSWORD-HERE line the container refuses to start and
# says exactly that, so a half-finished setup can never look like a wrong
# password.
set -euo pipefail

target="${1:-usms_pw}"

if [ -d "$target" ]; then
  echo "ERROR: $target is a DIRECTORY, not a file." >&2
  echo "       Docker creates an empty directory at a bind-mount path whose host file" >&2
  echo "       does not exist — that is also why the scraper says the path is a directory." >&2
  echo "       Remove it (rmdir '$target') and run this script again." >&2
  exit 1
fi

if [ -e "$target" ]; then
  echo "REFUSING to overwrite the existing $target — it may already hold the password." >&2
  echo "Delete it yourself if you really want a fresh template." >&2
  exit 1
fi

umask 077
cat > "$target" <<'TEMPLATE'
# ─────────────────────────────────────────────────────────────────────────────
# USMS scraper password  —  read by the container, never parsed by Compose.
#
# Replace the PASTE-YOUR-PASSWORD-HERE line below with the real password and
# save. Keep it a single line. Everything is safe in here: $, quotes, spaces,
# backslashes — Compose cannot mangle a file it never reads.
#
# Lines beginning with # and blank lines are ignored; the first other line is
# the password. A password whose own first character is # will be read as a
# comment — put that one in the environment with a $$ escape instead.
#
# Wire it up in compose.yaml:
#
#   services:
#     usms-scrape:
#       environment:
#         - USMS_USERNAME=01234567
#         - USMS_PASSWORD_FILE=/run/secrets/usms_pw
#       volumes:
#         - ./usms_pw:/run/secrets/usms_pw:ro
#
# The startup log then prints, with the length only, never the value:
#
#   USMS_PASSWORD loaded from /run/secrets/usms_pw (11 characters)
#   config: ... credentials username=set password=set (11 chars) ...
#
# If that length is shorter than the password you typed, something truncated it.
# ─────────────────────────────────────────────────────────────────────────────
PASTE-YOUR-PASSWORD-HERE
TEMPLATE

chmod 600 "$target"
echo "created $target (mode 600, owner-only)."
echo "Now edit it and replace the PASTE-YOUR-PASSWORD-HERE line, then:"
echo "  docker compose up -d --force-recreate usms-scrape"
