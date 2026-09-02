#!/usr/bin/env bash
# Starts the chat stack. Run from anywhere: ./scripts/start.sh
#
#   ./scripts/start.sh           start everything (ollama + api + ui)
#   ./scripts/start.sh --build   force a rebuild (needed after requirements.txt changes)
#   ./scripts/start.sh --stop    stop everything
#   ./scripts/start.sh --logs    follow logs
#
# Fully self-contained: Ollama runs in a container and the model lives in the
# ollama-data volume. Nothing needs to be installed on the host but Docker.
#
# Reuses the existing image by default. backend/ and frontend/ are bind-mounted
# (uvicorn --reload), so code edits need no rebuild -- only dependency changes do.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE=(docker compose -f DockerCompse.yml)
MODEL="${MODEL:-llama3.2:1b}"
BUILD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build) BUILD=1 ;;
    --stop)
      "${COMPOSE[@]}" down
      echo "stopped. (the model stays in the ollama-data volume)"
      exit 0
      ;;
    --logs)
      exec "${COMPOSE[@]}" logs -f
      ;;
    *)
      echo "unknown option: $1" >&2
      sed -n '2,13p' "${BASH_SOURCE[0]}" >&2
      exit 1
      ;;
  esac
  shift
done

if ! docker info >/dev/null 2>&1; then
  echo "Docker isn't running. Start Docker Desktop and try again." >&2
  exit 1
fi

# compose builds a missing image on its own, so the default path still works on
# a clean machine -- it just won't rebuild when one is already there.
UP=(up -d)
if [[ $BUILD -eq 1 ]]; then
  UP=(up --build -d)
  echo "==> rebuilding"
elif ! docker image inspect local-llama-api >/dev/null 2>&1; then
  echo "==> no image yet, building"
else
  echo "==> using existing image (--build to rebuild)"
fi

"${COMPOSE[@]}" "${UP[@]}"

# The volume survives --stop, so this only downloads on a genuinely fresh setup.
if ! "${COMPOSE[@]}" exec -T ollama ollama list 2>/dev/null | grep -q "$MODEL"; then
  echo "==> pulling $MODEL into the volume (~1.3 GB, first run only)"
  "${COMPOSE[@]}" exec -T ollama ollama pull "$MODEL"
fi

echo "==> waiting for the API"
for i in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    break
  fi
  if [[ $i -eq 60 ]]; then
    echo "API never came up. Logs:" >&2
    "${COMPOSE[@]}" logs --tail 40 api >&2
    exit 1
  fi
  sleep 1
done

# /health returns 200 even when the upstream is unreachable, so check the body.
if ! curl -fsS http://localhost:8000/health | grep -q '"ok":true'; then
  echo
  echo "WARNING: the API is up but can't reach Ollama:" >&2
  curl -fsS http://localhost:8000/health >&2
  echo >&2
fi

echo
echo "  UI      http://localhost:3000"
echo "  API     http://localhost:8000/docs"
echo "  Ollama  http://localhost:11434"
echo
echo "  logs  ./scripts/start.sh --logs"
echo "  stop  ./scripts/start.sh --stop"
