#!/usr/bin/env bash
# Container entrypoint: prepare the DB, then start monitoring.
#
# Discovery (UDP broadcast) does NOT work from a bridge network, so it's a HOST
# step — run `uv run python main.py discover --save` on the host to populate
# ./local/devices.json (mounted here) and the device/device_snapshot tables.
# Set RUN_DISCOVERY=true only if you've switched this service to host networking.
set -euo pipefail
cd /app

echo "[entrypoint] applying migrations..."
python main.py migrate up

if [ "${RUN_DISCOVERY:-false}" = "true" ]; then
    echo "[entrypoint] RUN_DISCOVERY=true — discovering (needs host networking)..."
    python main.py discover --save || echo "[entrypoint] discovery non-zero, continuing"
fi

# monitor needs a registry. On a bridge network it must come from host discovery.
if [ ! -f local/devices.json ]; then
    echo "[entrypoint] local/devices.json not found."
    echo "[entrypoint] Run on the HOST:  uv run python main.py discover --save"
    echo "[entrypoint] Waiting for the registry to appear..."
    while [ ! -f local/devices.json ]; do
        sleep 10
    done
fi

echo "[entrypoint] monitor devices='${MONITOR_DEVICES}' interval=${MONITOR_INTERVAL}s sample=${MONITOR_SAMPLE}s"
# MONITOR_DEVICES is intentionally unquoted so multiple devices/prefixes split into args.
exec python main.py monitor ${MONITOR_DEVICES} --interval "${MONITOR_INTERVAL}" --sample "${MONITOR_SAMPLE}"
