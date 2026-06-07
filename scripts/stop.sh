#!/bin/sh
# Stop the stack. Pass --volumes (or -v) to also drop the ClickHouse data volume.
# POSIX sh — runs under dash or bash.
set -eu
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--volumes" ] || [ "${1:-}" = "-v" ]; then
    echo "[stop] stopping stack and removing volumes (ClickHouse data will be lost)..."
    docker compose down --volumes
else
    echo "[stop] stopping stack (data volume kept)..."
    docker compose down
fi
