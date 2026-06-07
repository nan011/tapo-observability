#!/bin/sh
# Gateway to the main.py CLI INSIDE the running app container — forwards all
# args verbatim. Targets the container by the tapo-observability service's
# container_name in docker-compose.yml (via `docker exec`), so it works no
# matter which directory/compose-project launched the stack.
#   sh ./scripts/main.sh list
#   sh ./scripts/main.sh status all
#   sh ./scripts/main.sh migrate up
# Any command/flags main.py accepts work here. NOTE: `discover` needs LAN
# broadcast and won't work from the bridge-networked container — run that on the
# HOST:  uv run python main.py discover --save
# POSIX sh — runs under dash or bash.
set -eu
cd "$(dirname "$0")/.."

if [ ! -f docker-compose.yml ]; then
    echo "[main] docker-compose.yml not found." >&2
    echo "       Create it first: cp docker-compose.yml.example docker-compose.yml" >&2
    exit 1
fi

# Read the app service's container_name from docker-compose.yml.
CONTAINER="$(grep -A8 '^  tapo-observability:' docker-compose.yml | grep 'container_name:' | head -n1 | awk '{print $2}')"
CONTAINER="${CONTAINER:-tapo-observability}"

if [ -z "$(docker ps -q -f "name=^${CONTAINER}$")" ]; then
    echo "[main] container '${CONTAINER}' not running." >&2
    echo "       Start the stack first:  sh scripts/start.sh" >&2
    exit 1
fi

# Allocate a TTY only when stdin is one (interactive); plain pipe otherwise.
if [ -t 0 ]; then TTY=-it; else TTY=-i; fi

exec docker exec $TTY "$CONTAINER" python main.py "$@"
