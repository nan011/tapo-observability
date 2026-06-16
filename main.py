"""Tapo manager — control many Tapo devices (mixed types/IPs) from one CLI.

There is no device file. Every command DISCOVERS devices on the LAN in memory
(broadcast, or a unicast subnet scan via TAPO_SUBNET) and operates on that
list — targeting a device by name, device_id prefix, or `all`. Discovery also
mirrors device metadata into ClickHouse (device / device_snapshot).

Credentials come from the environment (see .env.example). Nothing hardcoded;
.env is gitignored. Device handler is chosen generically from the model:
ApiClient.<model.lower()>(ip) — so P115 -> p115, L530 -> l530, etc.
"""

import argparse
import asyncio
import ipaddress
import os
import socket
from datetime import datetime, timezone

from dotenv import load_dotenv
from tapo import ApiClient

import db


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


def _env_cidr() -> str | None:
    """Read the subnet to scan from TAPO_SUBNET (a CIDR), or None.

    Lets discovery run from a bridge-networked container: broadcast can't cross
    the bridge, but unicast can, so we sweep every host in this subnet instead.
    A bare network address (no `/prefix`) is treated as a /24.
    """
    v = os.getenv("TAPO_SUBNET")
    if not v:
        return None
    v = v.strip()
    return v if "/" in v else f"{v}/24"


def _hosts_in(cidr: str) -> list[str]:
    """Expand a CIDR (e.g. 192.168.1.0/24) into its usable host IPs."""
    net = ipaddress.ip_network(cidr, strict=False)
    return [str(h) for h in net.hosts()]


def _parse_result(maybe) -> dict | None:
    """Turn a MaybeDiscoveryResult into a registry entry, or None if undecodable."""
    try:
        d = maybe.get()  # MaybeDiscoveryResult -> DiscoveryResult
    except Exception:
        return None
    if d is None:
        return None
    dtype = str(d.device_type).rsplit(".", 1)[-1]  # DeviceType.Hub -> Hub
    return {
        "name": _slug(d.nickname, d.model, d.ip),
        "model": d.model,
        "type": dtype,
        "device_id": d.device_id,  # stable identity; survives IP changes
        "ip": d.ip,
    }


async def _discover_broadcast(client, target: str, timeout: int, quiet: bool) -> list[dict]:
    found: list[dict] = []
    async for maybe in await client.discover_devices(target, timeout_s=timeout):
        entry = _parse_result(maybe)
        if entry:
            found.append(entry)
            if not quiet:
                print(f"  {entry['ip']:<15} {entry['model']:<8} {entry['type']:<22} {entry['name']}")
    return found


async def _discover_scan(client, hosts: list[str], timeout: int, concurrency: int, quiet: bool) -> list[dict]:
    """Unicast-probe every host concurrently. Works over a bridge network where
    broadcast discovery can't reach the LAN. Dead hosts just time out and are
    skipped; only Tapo devices that reply are returned."""
    found: list[dict] = []
    sem = asyncio.Semaphore(concurrency)

    async def probe(ip: str) -> None:
        async with sem:
            try:
                results = await client.discover_devices(ip, timeout_s=timeout)
            except Exception:
                return  # no device / no reply at this IP
            async for maybe in results:
                entry = _parse_result(maybe)
                if entry:
                    found.append(entry)
                    if not quiet:
                        print(f"  {entry['ip']:<15} {entry['model']:<8} {entry['type']:<22} {entry['name']}")

    await asyncio.gather(*(probe(ip) for ip in hosts))
    return found


async def scan_devices(
    client: ApiClient,
    *,
    target: str | None = None,
    scan: bool = False,
    cidr: str | None = None,
    timeout: int = 2,
    concurrency: int = 64,
    sync_ch: bool = False,
    quiet: bool = False,
) -> list[dict]:
    """Discover devices and return them IN MEMORY — no file is written.

    Every command that needs the device list calls this and uses the result
    directly. Unicast-scans the TAPO_SUBNET subnet (works inside a bridge
    container) when a CIDR is configured and no broadcast target is given;
    otherwise broadcasts. With sync_ch=True it also mirrors the devices into
    ClickHouse (`device` / `device_snapshot`).
    """
    cidr = cidr or _env_cidr()
    use_scan = scan or (cidr is not None and target is None)

    if use_scan:
        if not cidr:
            raise SystemExit(
                "Scan mode needs a subnet: set TAPO_SUBNET (e.g. 192.168.1.0/24) or pass --cidr."
            )
        hosts = _hosts_in(cidr)
        if not quiet:
            print(f"Scanning {cidr} — {len(hosts)} hosts (unicast, timeout {timeout}s, {concurrency} at a time) ...")
        found = await _discover_scan(client, hosts, timeout, concurrency, quiet)
    else:
        target = target or _default_broadcast()
        if not quiet:
            print(f"Scanning {target} (broadcast, timeout {timeout}s) ...")
        found = await _discover_broadcast(client, target, timeout, quiet)

    if not quiet:
        print(f"{len(found)} device(s) found")
    if sync_ch:
        _sync_clickhouse(found)
    return found


def _sync_clickhouse(found: list[dict]) -> None:
    """Mirror an in-memory scan into ClickHouse: upsert every device into `device`
    (latest per id), and append a `device_snapshot` row for each device whose
    name/type/ip differs from what's already in `device` (or is newly seen).
    No-op if CH is unconfigured; never raises (scanning must work if CH is down).
    """
    if not found or not db.ch_configured():
        return
    try:
        client = db.get_client()
        prev = db.current_device_state(client)  # device_id -> (name, type, ip)
        changed = 0
        for d in found:
            did = d.get("device_id") or ""
            name = d.get("name") or ""
            type_ = d.get("type") or d.get("model") or ""
            ip = d.get("ip") or ""
            db.upsert_device(client, device_id=did, name=name, type=type_, ip=ip)
            if prev.get(did) != (name, type_, ip):
                db.insert_snapshot(client, device_id=did, name=name, type=type_, ip=ip)
                changed += 1
        print(f"ClickHouse: {len(found)} device(s) upserted, {changed} snapshot(s).")
    except Exception as e:  # scanning must work even if CH is down
        print(f"# clickhouse sync skipped: {e}")


async def list_devices() -> None:
    client = _client()
    devices = await scan_devices(client, quiet=True)
    for d in sorted(devices, key=lambda x: x["name"]):
        print(f"  {d['name']:<24} {d['model']:<8} {d['ip']:<15} {d.get('device_id', '-')}")


async def discover(
    target: str | None,
    timeout: int,
    scan: bool = False,
    cidr: str | None = None,
    concurrency: int = 64,
) -> None:
    """Scan and print devices to the console; mirror them into ClickHouse.

    Nothing is written to disk — the device list lives in memory. Commands that
    need it (monitor/status/on/off/list) run their own scan via `scan_devices`.
    """
    client = _client()
    await scan_devices(
        client, target=target, scan=scan, cidr=cidr,
        timeout=timeout, concurrency=concurrency, sync_ch=True,
    )


# --- per-device commands --------------------------------------------------


async def status(name: str) -> None:
    client = _client()
    targets = select(await scan_devices(client, quiet=True), name)
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
    client = _client()
    targets = select(await scan_devices(client, quiet=True), name)
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

    Discovers the devices in memory at startup (this is the per-boot discovery —
    it also mirrors device metadata into ClickHouse), then monitors them.
    """
    ch = db.get_client()  # CH-only sink; fails fast if unconfigured
    client = _client()
    targets = select_many(await scan_devices(client, sync_ch=True), names)
    energy = [d for d in targets if d["model"].lower() in ("p110", "p110m", "p115")]
    skipped = [d["name"] for d in targets if d not in energy]
    if skipped:
        print(f"# skipping non-energy devices: {', '.join(skipped)}", flush=True)
    if not energy:
        raise SystemExit("No energy-monitoring devices to watch.")

    sample_s = max(1, min(sample_s, interval))  # can't sample faster than 1s or slower than window
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

    disc = sub.add_parser("discover", help="Scan + print devices (and mirror to ClickHouse)")
    disc.add_argument("--target", help="Broadcast addr; auto-detected if omitted")
    disc.add_argument("--timeout", type=int, default=2, help="Per-target seconds (default 2)")
    disc.add_argument(
        "--scan", action="store_true",
        help="Unicast-sweep a subnet instead of broadcasting (works inside a bridge container)",
    )
    disc.add_argument(
        "--cidr",
        help="Subnet to scan, e.g. 192.168.1.0/24 (else from TAPO_SUBNET)",
    )
    disc.add_argument(
        "--concurrency", type=int, default=64, help="Parallel probes when scanning (default 64)"
    )

    sub.add_parser("list", help="Scan and list devices")

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
        asyncio.run(discover(
            args.target, args.timeout,
            scan=args.scan, cidr=args.cidr, concurrency=args.concurrency,
        ))
    elif args.cmd == "list":
        asyncio.run(list_devices())
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
