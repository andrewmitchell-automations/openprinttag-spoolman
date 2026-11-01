#!/bin/sh
# Docker creates a missing ./data folder as root, so hand it over before dropping privileges.
set -e
USER_ID="${PUID:-${UID:-1000}}"
GROUP_ID="${PGID:-${GID:-1000}}"
if [ "$(id -u)" = 0 ]; then
  mkdir -p "$DATA_DIR"
  chown -R "$USER_ID:$GROUP_ID" "$DATA_DIR"
  exec setpriv --reuid="$USER_ID" --regid="$GROUP_ID" --clear-groups python -m bridge "$@"
fi
exec python -m bridge "$@"
