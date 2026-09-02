#!/usr/bin/env bash
# Builds and starts the chat stack. Run from anywhere: ./scripts/start.sh
#
#   ./scripts/start.sh                 API + UI, talking to the Ollama on your Mac (fast, Metal)
#   ./scripts/start.sh --with-ollama   also runs Ollama in a container (CPU-only, slow)
#   ./scripts/start.sh --stop          stop everything
#   ./scripts/start.sh --logs          follow logs
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE=(docker compose -f DockerCompse.yml)
MODEL="${MODEL:-llama3.2:1b}"

case "${1:-}" in
  --stop)
    "${COMPOSE[@]}" --profile with-ollama down
    echo "stopped."
    exit 0
    ;;
  --logs)
    exec "${COMPOSE[@]}" logs -f
    ;;
esac

if ! docker info >/dev/null 2>&1; then
  echo "Docker isn't running. Start Docker Desktop and try again." >&2
  exit 1
fi

if [[ "${1:-}" == "--with-ollama" ]]; then
  # Containerised Ollama owns port 11434, so the brew service can't also hold it.
  if curl -fsS http://localhost:11434 >/dev/null 2>&1 && ! docker ps --format '{{.Names}}' | grep -q ollama; then
    echo "Port 11434 is held by the Ollama on your Mac. Stop it first:" >&2
    echo "  brew services stop ollama" >&2
    exit 1
  fi
  export OLLAMA_HOST="http://ollama:11434"
  WITH_OLLAMA=1
  echo "==> mode: Ollama in a container (CPU-only)"
else
  WITH_OLLAMA=0
  echo "==> mode: host Ollama (Metal-accelerated)"

  if ! curl -fsS http://localhost:11434 >/dev/null 2>&1; then
    echo "Ollama isn't running on the host. Starting it..."
    brew services start ollama
    for _ in $(seq 1 30); do
      curl -fsS http://localhost:11434 >/dev/null 2>&1 && break
      sleep 1
    done
  fi

  if ! ollama list 2>/dev/null | grep -q "$MODEL"; then
    echo "==> pulling $MODEL"
    ollama pull "$MODEL"
  fi
fi

echo "==> building"
if [[ $WITH_OLLAMA -eq 1 ]]; then
  "${COMPOSE[@]}" --profile with-ollama up --build -d
else
  "${COMPOSE[@]}" up --build -d
fi

if [[ $WITH_OLLAMA -eq 1 ]]; then
  echo "==> pulling $MODEL inside the container (first run downloads ~1.3 GB)"
  docker compose -f DockerCompse.yml exec -T ollama ollama pull "$MODEL"
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
echo "  UI   http://localhost:3000"
echo "  API  http://localhost:8000/docs"
echo
echo "  logs  ./scripts/start.sh --logs"
echo "  stop  ./scripts/start.sh --stop"
