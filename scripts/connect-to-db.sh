#!/bin/sh
# Open an interactive ClickHouse client against the running compose stack.
# Targets the clickhouse container by the container_name set in
# docker-compose.yml (via `docker exec`), so it connects no matter which
# directory/compose-project launched the stack. Works regardless of host port
# mappings. Any args are forwarded to the client, e.g. ad-hoc query:
#   ./scripts/connect-to-db.sh --query "SELECT 1"
# POSIX sh — runs under dash or bash.
set -eu
cd "$(dirname "$0")/.."

if [ ! -f docker-compose.yml ]; then
    echo "[connect-to-db] docker-compose.yml not found." >&2
    echo "                Create it first: cp docker-compose.yml.example docker-compose.yml" >&2
    exit 1
fi

# Read the tapo-clickhouse service's container_name from docker-compose.yml.
CONTAINER="$(grep -A8 '^  tapo-clickhouse:' docker-compose.yml | grep 'container_name:' | head -n1 | awk '{print $2}')"
CONTAINER="${CONTAINER:-tapo-clickhouse}"

if [ -z "$(docker ps -q -f "name=^${CONTAINER}$")" ]; then
    echo "[connect-to-db] container '${CONTAINER}' not running." >&2
    echo "                Start the stack first:  sh scripts/start.sh" >&2
    exit 1
fi

# Pull creds from .env if present. Don't source it (comments contain backticks);
# grep out just the keys we need.
env_val() {
    [ -f .env ] || return 0
    grep -E "^$1=" .env | tail -n1 | cut -d= -f2-
}
CLICKHOUSE_USER="${CLICKHOUSE_USER:-$(env_val CLICKHOUSE_USER)}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-$(env_val CLICKHOUSE_PASSWORD)}"
CLICKHOUSE_DATABASE="${CLICKHOUSE_DATABASE:-$(env_val CLICKHOUSE_DATABASE)}"

# Allocate a TTY only when stdin is one (interactive shell); plain pipe for --query.
if [ -t 0 ]; then TTY=-it; else TTY=-i; fi

exec docker exec $TTY "$CONTAINER" clickhouse-client \
    --user "${CLICKHOUSE_USER:-default}" \
    --password "${CLICKHOUSE_PASSWORD:-}" \
    --database "${CLICKHOUSE_DATABASE:-default}" \
    "$@"
