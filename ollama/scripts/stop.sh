#!/usr/bin/env bash
# Stops the chat stack. Run from anywhere: ./scripts/stop.sh
#
#   ./scripts/stop.sh            stop and remove the containers (model is kept)
#   ./scripts/stop.sh --purge    also delete the ollama-data volume (model re-downloads on next start)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE=(docker compose -f DockerCompse.yml)
PURGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE=1 ;;
    *)
      echo "unknown option: $1" >&2
      sed -n '2,5p' "${BASH_SOURCE[0]}" >&2
      exit 1
      ;;
  esac
  shift
done

if ! docker info >/dev/null 2>&1; then
  echo "Docker isn't running, so nothing to stop." >&2
  exit 0
fi

"${COMPOSE[@]}" down

# Only the model volume -- `down -v` would also wipe library-data.
if [[ $PURGE -eq 1 ]]; then
  docker volume ls -q \
    --filter label=com.docker.compose.project=local-llama \
    --filter label=com.docker.compose.volume=ollama-data \
    | xargs -r docker volume rm
  echo "stopped and removed ollama-data. (the model will be pulled again on next start)"
else
  echo "stopped. (the model stays in the ollama-data volume)"
fi
