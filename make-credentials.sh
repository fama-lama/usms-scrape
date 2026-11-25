#!/usr/bin/env bash
# Create the credentials file(s) the scraper reads at run time.
#
#   ./make-credentials.sh                       # ./usms_pw   (password only — the usual case)
#   ./make-credentials.sh my-password.txt        # choose the filename
#   ./make-credentials.sh usms_pw --with-username
#                                                # also writes ./usms_user (IC number)
#
# WHY A FILE AT ALL — Docker Compose INTERPOLATES `$` in every value it reads
# (environment:, env_file:, .env). A password like `$ecret1` arrives EMPTY and
# `MyP@ss$w0rd` arrives as `MyP@ss` — before the container ever starts. A file the
# scraper opens itself is never touched by Compose, so `$`, quotes, spaces and
# backslashes all survive.
#
# USE ONE ROUTE OR THE OTHER, NOT BOTH. If `USMS_PASSWORD` is also set in compose
# it WINS over `USMS_PASSWORD_FILE` and the file is ignored — the scraper warns
# when it sees both, but the real fix is to delete the environment line.
#
# The username (IC number) is digits only, so it normally lives happily in
# compose; --with-username exists for people who want nothing in the compose file.
set -euo pipefail

target="usms_pw"
want_username=0
for arg in "$@"; do
  case "$arg" in
    --with-username) want_username=1 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) target="$arg" ;;
  esac
done

write_template() {
  local path="$1" kind="$2" placeholder="$3" note="$4" var="$5"
  local base
  base="$(basename "$path")"

  if [ -d "$path" ]; then
    echo "ERROR: $path is a DIRECTORY, not a file." >&2
    echo "       Docker creates an empty directory at a bind-mount path whose host file" >&2
    echo "       does not exist — that is also why the scraper calls the path a directory." >&2
    echo "       Remove it (rmdir '$path') and run this script again." >&2
    exit 1
  fi
  if [ -e "$path" ]; then
    echo "REFUSING to overwrite the existing $path — it may already hold a real value." >&2
    echo "Delete it yourself if you really want a fresh template." >&2
    exit 1
  fi

  umask 077
  {
    echo '# ─────────────────────────────────────────────────────────────────────────────'
    echo "# USMS $kind — read by the container, never parsed by Docker Compose."
    echo '#'
    echo "# Replace the $placeholder line below with the real value and save."
    echo '# Keep it a single line. Everything is safe in here: $, quotes, spaces,'
    echo '# backslashes — Compose cannot mangle a file it never reads.'
    echo '#'
    echo '# Lines beginning with # and blank lines are ignored; the first other'
    echo '# line is the value.'
    echo '#'
    [ -n "$note" ] && printf '%s' "$note"
    echo '#'
    echo '# Wire it up in compose.yaml — and DELETE the matching environment'
    echo "# variable ($var), because that one wins and would override this file:"
    echo '#'
    echo '#   services:'
    echo '#     usms-scrape:'
    echo '#       environment:'
    echo "#         - ${var}_FILE=/run/secrets/$base"
    echo '#       volumes:'
    echo "#         - ./$base:/run/secrets/$base:ro"
    echo '#'
    echo '# Startup then logs the length only, never the value:'
    echo "#   $var loaded from /run/secrets/$base (11 characters)"
    echo '#'
    echo '# If that length is shorter than what you typed, something truncated it.'
    echo '# ─────────────────────────────────────────────────────────────────────────────'
    echo "$placeholder"
  } > "$path"
  chmod 600 "$path"
  echo "created $path (mode 600, owner-only) for $var"
}

write_template "$target" "password" "PASTE-YOUR-PASSWORD-HERE" "" "USMS_PASSWORD"

if [ "$want_username" -eq 1 ]; then
  user_path="usms_user"
  if [ "$(dirname "$target")" != "." ]; then
    user_path="$(dirname "$target")/usms_user"
  fi
  write_template "$user_path" "username (IC number)" "PASTE-YOUR-IC-NUMBER-HERE" \
'# The IC number is digits only, so it normally lives in compose quite happily.
# Use this file only if you want nothing at all in the compose environment.
' "USMS_USERNAME"
fi

echo
echo "NEXT: edit the file(s), replace the placeholder, then"
echo "  docker compose up -d --force-recreate usms-scrape"
echo
echo "REMEMBER: one route only. If the same value is also in compose as an"
echo "environment variable, that wins and the file is ignored."
