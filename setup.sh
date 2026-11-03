#!/usr/bin/env bash
# Starts the bridge, or updates it to the newest release. The rest of the setup
# happens in the web interface.
#
#   ./setup.sh           pull the published image and (re)start
#   ./setup.sh --build   build the image from this checkout instead
set -euo pipefail

cd "$(dirname "$0")"

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
else
  echo "Docker needs sudo here, so you may be asked for your password."
  DOCKER=(sudo docker)
fi
"${DOCKER[@]}" compose version >/dev/null 2>&1 || { echo "ERROR: Docker Compose is missing." >&2; exit 1; }

case "${1:-}" in
  --build)
    COMPOSE=("${DOCKER[@]}" compose -f docker-compose.yml -f docker-compose.build.yml)
    "${COMPOSE[@]}" up -d --build
    ;;
  "")
    COMPOSE=("${DOCKER[@]}" compose)
    "${COMPOSE[@]}" pull
    "${COMPOSE[@]}" up -d
    ;;
  *)
    echo "Usage: $0 [--build]" >&2
    exit 2
    ;;
esac

host=$(hostname -I 2>/dev/null | awk '{print $1}')
port=$(sed -n 's/^WEB_PORT=//p' .env 2>/dev/null | tail -n 1)
echo
echo "The bridge is running. Finish the setup at http://${host:-your-server}:${port:-7913}"
echo "Follow the log with: ${COMPOSE[*]} logs -f"
