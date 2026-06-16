#!/usr/bin/env bash
# Container entrypoint: prepare the DB, then start monitoring.
#
# No device file is used. `monitor` DISCOVERS devices in memory at startup (every
# boot) and monitors them, mirroring device metadata into ClickHouse. On the
# default bridge network, broadcast can't reach the LAN — so set TAPO_SUBNET
# (a CIDR, e.g. 192.168.1.0/24) and discovery unicast-scans that subnet, which
# DOES cross the bridge. (On host networking, broadcast works without TAPO_SUBNET.)
set -euo pipefail
cd /app

echo "[entrypoint] applying migrations..."
python main.py migrate up

echo "[entrypoint] monitor devices='${MONITOR_DEVICES}' interval=${MONITOR_INTERVAL}s sample=${MONITOR_SAMPLE}s"
# monitor scans for devices first (this is the per-boot discovery), then runs.
# MONITOR_DEVICES is intentionally unquoted so multiple devices/prefixes split into args.
exec python main.py monitor ${MONITOR_DEVICES} --interval "${MONITOR_INTERVAL}" --sample "${MONITOR_SAMPLE}"
