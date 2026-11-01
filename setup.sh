#!/usr/bin/env bash
# Builds and starts the bridge. The rest of the setup happens in the web interface.
set -euo pipefail

cd "$(dirname "$0")"

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
else
  echo "Docker needs sudo here, so you may be asked for your password."
  DOCKER=(sudo docker)
fi
"${DOCKER[@]}" compose version >/dev/null 2>&1 || { echo "ERROR: Docker Compose is missing." >&2; exit 1; }

"${DOCKER[@]}" compose up -d --build

host=$(hostname -I 2>/dev/null | awk '{print $1}')
port=$(sed -n 's/^WEB_PORT=//p' .env 2>/dev/null | tail -n 1)
echo
echo "The bridge is running. Finish the setup at http://${host:-your-server}:${port:-7913}"
echo "Follow the log with: ${DOCKER[*]} compose logs -f"
