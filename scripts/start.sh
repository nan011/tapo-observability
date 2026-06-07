#!/bin/sh
# Pull any missing/updated images, tear the stack down, then bring it back up.
# Requires docker-compose.yml (copy it from docker-compose.yml.example first).
# POSIX sh — runs under dash or bash.
set -eu
cd "$(dirname "$0")/.."

if [ ! -f docker-compose.yml ]; then
    echo "[start] docker-compose.yml not found." >&2
    echo "        Create it first: cp docker-compose.yml.example docker-compose.yml" >&2
    exit 1
fi

echo "[start] pulling images..."
docker compose pull --ignore-buildable

echo "[start] stopping stack..."
docker compose down

echo "[start] starting stack (build + detach)..."
docker compose up --build -d

echo "[start] up. Following app logs (Ctrl-C to detach; containers keep running)..."
docker compose logs -f tapo-observability
