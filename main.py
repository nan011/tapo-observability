"""Tapo manager — control many Tapo devices (mixed types/IPs) from one CLI.

Devices live in a registry file (./local/devices.json): name + model + ip. `discover`
finds them on the LAN and can save the registry. All other commands target a
device by name, or `all`.

Credentials come from the environment (see .env.example). Nothing hardcoded;
.env is gitignored. Device handler is chosen generically from the model:
ApiClient.<model.lower()>(ip) — so P115 -> p115, L530 -> l530, etc.
"""

import argparse
import asyncio
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from tapo import ApiClient

import db

REGISTRY = Path(__file__).parent / "local/devices.json"


# --- config / creds -------------------------------------------------------


def _require_creds() -> tuple[str, str]:
    load_dotenv()
    username = os.getenv("TAPO_USERNAME")
    password = os.getenv("TAPO_PASSWORD")
    if not username or not password:
        raise SystemExit("Missing TAPO_USERNAME / TAPO_PASSWORD (see .env.example)")
    return username, password


def _client() -> ApiClient:
    return ApiClient(*_require_creds())


def load_registry() -> list[dict]:
    if not REGISTRY.exists():
        raise SystemExit(f"No registry yet. Run `discover --save` first ({REGISTRY.name}).")
    return json.loads(REGISTRY.read_text())


def select(devices: list[dict], query: str) -> list[dict]:
    """Resolve a reference to device(s): 'all', exact name, or device_id prefix.

    device_id prefix works like git short SHAs (case-insensitive). A prefix that
    matches >1 device is rejected — caller must lengthen it.
    """
    if query == "all":
        return devices

    q = query.strip()
    by_name = [d for d in devices if d.get("name") == q]
    if by_name:
        return by_name

    qu = q.upper()
    by_id = [d for d in devices if (d.get("device_id") or "").upper().startswith(qu)]
    if len(by_id) == 1:
        return by_id
    if len(by_id) > 1:
        rows = "\n".join(f"  {d['device_id']}  {d['name']}" for d in by_id)
        raise SystemExit(
            f"Ambiguous device id prefix '{query}' matches {len(by_id)} devices:\n"
            f"{rows}\nUse a longer prefix."
        )

    names = ", ".join(d["name"] for d in devices) or "(none)"
    raise SystemExit(f"Unknown device '{query}'. Known: {names}")


def select_many(devices: list[dict], queries: list[str]) -> list[dict]:
    """Resolve several references into a de-duplicated device list (order-stable)."""
    out: list[dict] = []
    seen: set = set()
    for q in queries:
        for d in select(devices, q):
            key = d.get("device_id") or d.get("name") or id(d)
            if key not in seen:
                seen.add(key)
                out.append(d)
    return out


# --- generic handler ------------------------------------------------------


async def handler_for(client: ApiClient, device: dict):
    """Build the right device handler from its model via ApiClient.<model>()."""
    method = getattr(client, device["model"].lower(), None)
    if method is None:
        raise SystemExit(f"Unsupported model '{device['model']}' for {device['name']}")
    return await method(device["ip"])


# --- discovery ------------------------------------------------------------


def _default_broadcast() -> str:
    """Guess LAN broadcast from the primary interface (assumes /24)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet sent; just picks the egress IP
        local_ip = s.getsockname()[0]
    finally:
        s.close()
    return local_ip.rsplit(".", 1)[0] + ".255"


def _slug(nickname: str, model: str, ip: str) -> str:
    base = (nickname or model).strip().lower().replace(" ", "-")
    return base or ip.replace(".", "-")


async def discover(target: str | None, save: bool, timeout: int) -> None:
    target = target or _default_broadcast()
    client = _client()
    print(f"Scanning {target} (timeout {timeout}s) ...")
    found: list[dict] = []
    undecodable = 0
    devices = await client.discover_devices(target, timeout_s=timeout)
    async for maybe in devices:
        try:
            d = maybe.get()  # MaybeDiscoveryResult -> DiscoveryResult
        except Exception as e:
            # a device replied but its discovery payload couldn't be decoded —
            # surface it so it isn't silently lost (often a different creds/region)
            undecodable += 1
            print(f"  {'?':<15} {'?':<8} {'UNDECODABLE':<22} {e}")
            continue
        if d is None:
            undecodable += 1
            print(f"  {'?':<15} {'?':<8} {'UNDECODABLE':<22} (no result)")
            continue
        dtype = str(d.device_type).rsplit(".", 1)[-1]  # DeviceType.Hub -> Hub
        entry = {
            "name": _slug(d.nickname, d.model, d.ip),
            "model": d.model,
            "type": dtype,
            "device_id": d.device_id,  # stable identity; survives IP changes
            "ip": d.ip,
        }
        found.append(entry)
        print(f"  {d.ip:<15} {d.model:<8} {dtype:<22} {entry['name']}")
    msg = f"{len(found)} device(s) found"
    if undecodable:
        msg += f", {undecodable} replied but undecodable"
    print(msg)

    if save and found:
        _merge_save(found)


def _merge_save(found: list[dict]) -> None:
    """Merge discovery results into the registry, keyed by stable device_id.

    Updates IPs for known devices, keeps user-edited names, adds new devices.
    Existing devices not seen this scan are kept (might be temporarily offline).
    Legacy entries without a device_id are migrated by matching name or IP.
    """
    existing = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else []
    by_id = {d["device_id"]: d for d in existing if d.get("device_id")}
    legacy = [d for d in existing if not d.get("device_id")]  # pre-device_id entries

    def pop_legacy(e: dict) -> dict | None:
        """Find+remove a legacy entry matching this device by name or IP."""
        for i, old in enumerate(legacy):
            if old.get("name") == e["name"] or old.get("ip") == e["ip"]:
                return legacy.pop(i)
        return None

    added, changed = 0, 0
    snapshots: list[dict] = []  # devices whose name/ip/type changed (or are new)
    for e in found:
        legacy_match = pop_legacy(e)  # always pop so stale dupes are cleared
        prev = by_id.get(e["device_id"]) or legacy_match
        if prev is None:
            by_id[e["device_id"]] = e
            added += 1
            snapshots.append(e)
        else:
            # discovery is authoritative — refresh name/model/type/ip per device_id
            if (prev.get("name"), prev.get("type"), prev.get("ip")) != (
                e["name"], e["type"], e["ip"]
            ):
                changed += 1
                snapshots.append(e)
            prev["name"] = e["name"]
            prev["model"] = e["model"]
            prev["type"] = e["type"]
            prev["ip"] = e["ip"]
            prev["device_id"] = e["device_id"]
            by_id[e["device_id"]] = prev

    merged = list(by_id.values()) + legacy  # legacy = devices not seen this scan
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(merged, indent=2) + "\n")
    print(f"Registry: {len(merged)} total ({added} new, {changed} updated) -> {REGISTRY.name}")
    _sync_clickhouse(found, snapshots)


def _sync_clickhouse(all_devices: list[dict], changed: list[dict]) -> None:
    """Mirror discovery into ClickHouse: refresh `device` (latest per id) for all
    seen devices, and append a `device_snapshot` row for each changed/new device.
    No-op if CH is unconfigured; never breaks discovery if CH is down.
    """
    if not all_devices or not db.ch_configured():
        return
    try:
        client = db.get_client()
        for d in all_devices:
            db.upsert_device(
                client,
                device_id=d.get("device_id") or "",
                name=d.get("name") or "",
                type=d.get("type") or d.get("model") or "",
                ip=d.get("ip") or "",
            )
        for d in changed:
            db.insert_snapshot(
                client,
                device_id=d.get("device_id") or "",
                name=d.get("name") or "",
                type=d.get("type") or d.get("model") or "",
                ip=d.get("ip") or "",
            )
        print(f"ClickHouse: {len(all_devices)} device(s) upserted, {len(changed)} snapshot(s).")
    except Exception as e:  # discovery must work even if CH is down
        print(f"# clickhouse sync skipped: {e}")


def list_devices() -> None:
    for d in load_registry():
        print(f"  {d['name']:<24} {d['model']:<8} {d['ip']:<15} {d.get('device_id', '-')}")


# --- per-device commands --------------------------------------------------


async def status(name: str) -> None:
    targets = select(load_registry(), name)
    client = _client()
    for d in targets:
        try:
            h = await handler_for(client, d)
            info = await h.get_device_info()
            state = "ON " if getattr(info, "device_on", None) else "OFF"
            if not hasattr(info, "device_on"):
                state = "-  "  # hubs/sensors have no on/off state
            line = f"{d['name']:<24} {d['model']:<8} {state}"
            if hasattr(h, "get_current_power"):
                power = await h.get_current_power()
                line += f"  {power.current_power} W"
            print(line)
        except Exception as e:  # one bad device shouldn't abort the rest
            print(f"{d['name']:<20} {d['model']:<8} ERROR: {e}")


async def set_power(name: str, turn_on: bool) -> None:
    targets = select(load_registry(), name)
    client = _client()
    for d in targets:
        try:
            h = await handler_for(client, d)
            if not hasattr(h, "on"):
                print(f"{d['name']:<20} (no on/off — skipped)")
                continue
            await (h.on() if turn_on else h.off())
            print(f"{d['name']:<20} -> {'ON' if turn_on else 'OFF'}")
        except Exception as e:
            print(f"{d['name']:<20} ERROR: {e}")


# --- monitoring -----------------------------------------------------------


async def _sample(client: ApiClient, device: dict, cache: dict) -> float | None:
    """Read one instantaneous power value.

    On failure (most often SESSION_TIMEOUT — the plug expires the KLAP session),
    drop the cached handler and retry once: that rebuilds the handler, which
    re-authenticates from scratch, so a stale session self-heals *within the same
    sample* instead of losing it. Returns None only if the retry also fails.
    """
    name = device["name"]
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            h = cache.get(name)
            if h is None:
                h = cache[name] = await handler_for(client, device)  # (re)authenticate
            power = await h.get_current_power()
            return float(power.current_power)
        except Exception as e:
            last_err = e
            cache.pop(name, None)  # force re-auth on the retry / next sample
    print(f"# {name} sample error (after re-auth retry): {last_err}", flush=True)
    return None


async def _device_loop(
    client: ApiClient, ch, device: dict, interval: int, sample_s: int
) -> None:
    """Per-device loop: sample every `sample_s` for `interval` seconds, then insert
    the MEAN power for that window. One task per device — isolated cadence.

    Averaging matters when `interval` is large: a single instantaneous reading
    every hour is noisy; the mean of many samples over the window is representative.
    """
    cache: dict = {}
    n = max(1, round(interval / sample_s))  # samples per window
    prev_ts: datetime | None = None  # close time of the last row written for this device
    while True:
        samples: list[float] = []
        for i in range(n):
            v = await _sample(client, device, cache)
            if v is not None:
                samples.append(v)
            if i < n - 1:
                await asyncio.sleep(sample_s)
        ts = datetime.now(timezone.utc)  # window close time
        if not samples:
            print(f"# {ts.isoformat()} {device['name']} no samples this window", flush=True)
            continue
        mean = sum(samples) / len(samples)
        # Actual seconds since the previous row's power_used_at — the span this
        # mean represents for energy (kWh = power_used * window_seconds / 3.6e6).
        # Kept fractional (the column is Decimal32(3)); first row has no
        # predecessor, so fall back to the nominal interval.
        if prev_ts is None:
            window_seconds = float(interval)
        else:
            window_seconds = (ts - prev_ts).total_seconds()
        db.insert_power(
            ch,
            device_id=device.get("device_id") or "",
            power_used=mean,
            power_used_at=ts,
            window_seconds=window_seconds,
        )
        prev_ts = ts
        print(
            f"# {ts.isoformat()} {device['name']} mean {mean:.1f}W "
            f"({len(samples)}/{n} samples, {window_seconds:.3f}s window) -> clickhouse",
            flush=True,
        )


async def monitor(names: list[str], interval: int, sample_s: int) -> None:
    """Watch energy-capable devices, writing the mean power per `interval` to ClickHouse.

    Each device runs in its own asyncio task — a slow or failing device never
    delays or stops the others. Writes to device_power_usage only; requires
    ClickHouse to be configured.
    """
    targets = select_many(load_registry(), names)
    energy = [d for d in targets if d["model"].lower() in ("p110", "p110m", "p115")]
    skipped = [d["name"] for d in targets if d not in energy]
    if skipped:
        print(f"# skipping non-energy devices: {', '.join(skipped)}", flush=True)
    if not energy:
        raise SystemExit("No energy-monitoring devices to watch.")

    sample_s = max(1, min(sample_s, interval))  # can't sample faster than 1s or slower than window
    ch = db.get_client()  # CH-only sink; fails fast if unconfigured
    client = _client()
    print(
        f"# monitoring {len(energy)} device(s): mean of ~{max(1, round(interval / sample_s))} "
        f"samples every {interval}s (sample {sample_s}s) -> ClickHouse — Ctrl-C to stop",
        flush=True,
    )
    await asyncio.gather(*(_device_loop(client, ch, d, interval, sample_s) for d in energy))


# --- cli ------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Tapo multi-device manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    disc = sub.add_parser("discover", help="Find Tapo devices on the LAN")
    disc.add_argument("--target", help="Broadcast addr; auto-detected if omitted")
    disc.add_argument("--save", action="store_true", help="Write results to ./local/devices.json")
    disc.add_argument("--timeout", type=int, default=5, help="Scan seconds (default 5)")

    sub.add_parser("list", help="List registered devices")

    st = sub.add_parser("status", help="Show state (+power) for a device or 'all'")
    st.add_argument("name", nargs="?", default="all", help="Name, id prefix, or 'all'")

    on = sub.add_parser("on", help="Turn a device (or 'all') on")
    on.add_argument("name", help="Name, id prefix, or 'all'")
    off = sub.add_parser("off", help="Turn a device (or 'all') off")
    off.add_argument("name", help="Name, id prefix, or 'all'")

    mon = sub.add_parser("monitor", help="Average power per interval into ClickHouse")
    mon.add_argument("names", nargs="*", default=["all"], help="Device name(s) or id prefix(es)")
    mon.add_argument(
        "--interval", type=int, default=300, help="Window seconds; one mean row per window (default 300)"
    )
    mon.add_argument(
        "--sample", type=int, default=5, help="Seconds between samples within a window (default 5)"
    )

    mig = sub.add_parser("migrate", help="ClickHouse migrations")
    migsub = mig.add_subparsers(dest="migcmd", required=True)
    up = migsub.add_parser("up", help="Apply all pending migrations")
    up.add_argument("--fake", action="store_true", help="Record as applied without running SQL")
    down = migsub.add_parser("down", help="Roll back the N most recent migrations")
    down.add_argument("steps", type=int, help="How many migrations to roll back")
    down.add_argument("--fake", action="store_true", help="Remove from history without running SQL")
    migsub.add_parser("status", help="Show applied/pending migrations")

    args = parser.parse_args()

    if args.cmd == "discover":
        asyncio.run(discover(args.target, args.save, args.timeout))
    elif args.cmd == "list":
        list_devices()
    elif args.cmd == "status":
        asyncio.run(status(args.name))
    elif args.cmd == "on":
        asyncio.run(set_power(args.name, True))
    elif args.cmd == "off":
        asyncio.run(set_power(args.name, False))
    elif args.cmd == "monitor":
        names = args.names or ["all"]
        try:
            asyncio.run(monitor(names, args.interval, args.sample))
        except KeyboardInterrupt:
            print("\n# stopped")
    elif args.cmd == "migrate":
        if args.migcmd == "up":
            db.migrate_up(fake=args.fake)
        elif args.migcmd == "down":
            db.migrate_down(args.steps, fake=args.fake)
        elif args.migcmd == "status":
            db.migration_status()


if __name__ == "__main__":
    main()
