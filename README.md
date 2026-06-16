# Tapo Observability

**Power observability for TP-Link Tapo smart plugs.** Continuously samples real
power draw from your energy-monitoring plugs (P110/P115/…) and lands a clean time
series in **ClickHouse** — mean watts per window (with the window length stored
for exact kWh), partitioned by month, keyed by device — ready to chart in
Metabase/Grafana or query directly. The whole stack (ClickHouse + collector)
runs from one `docker compose`.

Built on [mihai-dinculescu/tapo](https://github.com/mihai-dinculescu/tapo)
(Rust core, Python bindings). Talks to plugs **locally** over the LAN (KLAP) — no
cloud round-trip. The device handler is chosen generically from each model
(`ApiClient.<model>()`), so P115, L530, P110, H100, etc. work without per-type code.

## Tiny on disk

**One plug sampled every 5 seconds logs ~7.7 million readings a year in ~50 MiB —
three full years still fits in ~150 MiB.** This is built to run for years on a
Raspberry Pi SD card or a small VPS without thinking about storage.

Measured over a steady 12-hour window (3 × P115 @ 5s): **~21,200 rows/day per
device**, each compressing to **~6.6 bytes** on disk (38% of raw) — about
**~140 KiB/day per device**. The table pairs **row count** with on-disk size:

| Span | **Rows / device** | **On disk / device** | Rows (3 devices) | On disk (3 devices) |
|---|--:|--:|--:|--:|
| 1 day | **~21 K** | **~140 KiB** | ~64 K | ~410 KiB |
| 1 month | **~640 K** | **~4 MiB** | ~1.9 M | ~12 MiB |
| 1 year | **~7.7 M** | **~50 MiB** | ~23 M | ~150 MiB |
| 3 years | **~23 M** | **~150 MiB** | ~70 M | ~440 MiB |

> **Per device: a plug logged every 5s for a whole year ≈ 7.7 million rows in
> ~50 MiB — three full years still fits in ~150 MiB.**

Tens of millions of rows, and the database is still smaller than a phone photo.
Sample slower (e.g. `--interval 30`) and both row count and size shrink
proportionally.

Why so small:
- **Minimal column types** — `LowCardinality(String)` for device_id/name/type,
  `Decimal32(3)` for power and window, `DateTime` (4B) for timestamps, `IPv4`
  (4B) for addresses. No oversized `Float64`/`UUID`/`String` waste.
- **ClickHouse columnar compression** — repetitive columns crush to a fraction
  of their raw size.
- **System logs disabled** — ClickHouse's own `system.*_log` tables (query_log,
  trace_log, part_log, …) normally grow unbounded inside the data volume; they're
  turned off (see [Disk usage](#disk-usage)), so the volume holds *your* data and
  almost nothing else.

## What it can do

Observability is the headline, but the same CLI (`main.py`) is a full Tapo
manager. Entry points:

| Command | What it does |
|---|---|
| `monitor` | **Observability** — sample power, write the per-interval mean to ClickHouse (`device_power_usage`). One async task per device, self-healing. |
| `discover` | Scan the LAN and **print the device list** — UDP broadcast, or `--scan` to unicast-sweep a subnet (works inside a bridge container). Also mirrors `device` / `device_snapshot` into ClickHouse. |
| `list` | Show registered devices (name, model, ip, device_id). |
| `status` | Live state + current power for a device or `all`. |
| `on` / `off` | **Control** — switch a plug (or `all`) on/off. |
| `migrate up/down/status` | ClickHouse schema migrations (`--fake` manages history only). |

Every command discovers devices on the LAN (broadcast, or a unicast subnet scan —
see below) and keeps the list **in memory for that run**; `discover`/`list` also
print it to the console. Any command targets a device by name, `device_id` prefix
(git short-SHA style), or `all`. `.env` holds your TP-Link account email/password
and the scan subnet (`TAPO_SUBNET`) — gitignored, never commit it.

## Getting Started

Brings up ClickHouse + this service on a Docker **bridge** network. The app
reaches ClickHouse by service name and reaches Tapo devices by unicast IP (the
host NATs the container out to your LAN), so `monitor` works without host
networking.

**Discovery runs in memory** — `monitor` discovers devices at startup (every
boot), holds the list in memory, and monitors them, mirroring `device` /
`device_snapshot` into ClickHouse. UDP broadcast can't cross the bridge, so set
`TAPO_SUBNET` (a CIDR like `192.168.1.0/24`) in `.env` and discovery
**unicast-scans** that subnet, which *does* cross the bridge. It re-scans on
every `docker compose up`, so device IP changes self-heal.

```bash
cp .env.example .env                              # set TAPO creds + TAPO_SUBNET + MONITOR_* params
cp docker-compose.yml.example docker-compose.yml  # one-time; edit freely (gitignored)

sh scripts/start.sh                               # build, down, up --build, follow logs
```

The container runs migrations, then starts `monitor` — which scans
`TAPO_SUBNET` for devices, keeps them in memory, and begins writing per-window
mean power into `device_power_usage`. No pre-seeding step.

`docker-compose.yml` is gitignored so your local edits stay out of version
control — copy it from the committed `docker-compose.yml.example` once, before
the first `start.sh`.

Configure the run via `.env`:

- `TAPO_SUBNET` — subnet (CIDR) to unicast-scan for devices (required in the container)
- `MONITOR_DEVICES` — which devices: name(s)/id prefix(es), space-separated, or `all`
- `MONITOR_INTERVAL` — window seconds; one **mean** row per window per device
- `MONITOR_SAMPLE` — seconds between samples inside the window

> **Prefer broadcast inside the container?** Switch the service to
> `network_mode: host` (Linux only; reach ClickHouse via `127.0.0.1`) and leave
> `TAPO_SUBNET` unset — `monitor` then discovers via broadcast.

`scripts/start.sh` pulls images, runs `docker compose down`, brings the stack up
with `--build -d`, and tails the app logs (it errors out if `docker-compose.yml`
is missing — copy it from the example first). `scripts/stop.sh` stops the stack
(`down`); pass `-v`/`--volumes` to also drop the ClickHouse data volume.

`scripts/connect-to-db.sh` opens an interactive `clickhouse-client` inside the
running `tapo-clickhouse` container (found by its `container_name`, no host port
needed). Args pass through, so you can also run ad-hoc queries:

```bash
sh scripts/connect-to-db.sh                          # interactive shell
sh scripts/connect-to-db.sh --query "SELECT version()"
sh scripts/connect-to-db.sh --query "SELECT * FROM device_power_usage LIMIT 5"
```

It reads `CLICKHOUSE_USER`/`CLICKHOUSE_PASSWORD`/`CLICKHOUSE_DATABASE` from `.env`
(falling back to `default`/empty/`default`).


## Run Locally

The Docker flow above needs no Python on the host. To run the `main.py` CLI
directly — host-run `discover`/`list`/`status`/`on`/`off`/`monitor`, or
development — install the deps with [uv](https://docs.astral.sh/uv/):

```bash
uv sync                 # install deps (tapo, clickhouse-connect, python-dotenv)
cp .env.example .env    # then edit .env with your TP-Link creds (+ CLICKHOUSE_* if using the DB)

uv run python main.py discover    # broadcast discovery works on the host
uv run python main.py status all
```

## Usage

`scripts/main.sh` is a gateway to the `main.py` CLI **inside the running app
container** — it `docker exec`s into `tapo-observability` and forwards every arg:

```bash
sh ./scripts/main.sh list
sh ./scripts/main.sh status all
sh ./scripts/main.sh migrate up
```

Every command discovers devices first and holds them in memory for that run, then
acts. **`discover`** is the one that cares about networking: broadcast needs the
host (or host networking), but `--scan` unicast-sweeps a subnet and works
straight from the bridge container:

```bash
sh ./scripts/main.sh discover --scan          # uses TAPO_SUBNET from .env
sh ./scripts/main.sh discover --scan --cidr 192.168.1.0/24
sh ./scripts/main.sh list                     # same scan, then lists
```

```bash
# discover: scan + print devices, mirror metadata to ClickHouse
uv run python main.py discover                          # broadcast (host); auto-detects /24
uv run python main.py discover --target 192.168.1.255  # broadcast to an explicit address
uv run python main.py discover --scan --cidr 192.168.1.0/24  # unicast sweep (works in-container)

# inspect / control by name (or 'all') — each scans first
uv run python main.py list             # discover + show devices
uv run python main.py status           # state (+power) for every device
uv run python main.py status office    # one device
uv run python main.py on office        # turn a device on
uv run python main.py off all          # turn everything that supports it off

# write per-window mean power to ClickHouse, logging one '# ...' line per row
uv run python main.py monitor                        # all energy plugs
uv run python main.py monitor pc-plug --interval 30  # one device, every 30s
uv run python main.py monitor 80226 8022B            # several devices at once
```

### Referencing devices

Any command that names a device accepts: `all`, the exact name, or a
**device_id prefix** (git short-SHA style, case-insensitive). A prefix matching
more than one device is rejected with the candidates listed — lengthen it:

```bash
uv run python main.py on 8022B     # unique prefix -> that device
uv run python main.py on 8022      # error: matches 3, use a longer prefix
```

`monitor` takes multiple references (names and/or prefixes), de-duplicated.

`monitor` samples `get_current_power()` every `--sample` seconds for an
`--interval` window, then **inserts the mean** of that window into ClickHouse
(`device_power_usage`) — one asyncio task per device, own cadence. Each row also
stores `window_seconds`: the **actual** elapsed seconds since that device's
previous row (not the nominal `--interval`), so `power_used * window_seconds`
integrates to energy with no drift across gaps. Averaging matters when the
interval is large: a single instantaneous reading per hour is noisy; the window
mean is representative. Dead sessions self-heal (handler rebuilt next sample);
one device erroring doesn't stop the others. ClickHouse must be configured —
`monitor` is the DB writer.

```bash
uv run python main.py monitor all --interval 300 --sample 5   # mean of ~60 samples per 5 min
uv run python main.py monitor pc-plug 8022B --interval 60      # two devices, 1-min means
```

## ClickHouse

Set `CLICKHOUSE_*` in `.env` (only `CLICKHOUSE_HOST` is required). Three tables,
created by the first migration. Column types are picked for minimal size (see
[Tiny on disk](#tiny-on-disk)).

#### `device_power_usage`
One row per power reading written by `monitor`.
`MergeTree` · partition `toYYYYMM(power_used_at)` · primary key `(device_id, power_used_at)`

| Column | Type | Meaning |
|---|---|---|
| `device_id` | `LowCardinality(String)` | which device |
| `power_used` | `Decimal32(3)` | **mean** watts over the window |
| `power_used_at` | `DateTime` | window close time |
| `window_seconds` | `Decimal32(3)` | measured seconds since the device's previous row |
| `created_at` | `DateTime` | DB insert time (default `now()`) |

Energy: `kWh = power_used * window_seconds / 3600 / 1000`. Both operands are
`Decimal32(3)`; their product keeps only 3 integer digits and overflows past 999
— **cast to float** in queries:
`sum(toFloat64(power_used) * toFloat64(window_seconds)) / 3.6e6`.

#### `device_snapshot`
Append-only metadata history — a row is added on any scan whenever a device's
name/type/ip differs from what's in `device` (and once when first seen).
`MergeTree` · primary key `(device_id, created_at)`

| Column | Type | Meaning |
|---|---|---|
| `id` | `UUID` | row id (UUIDv7, default) |
| `device_id` | `LowCardinality(String)` | which device |
| `created_at` | `DateTime` | when recorded (default `now()`) |
| `name` | `LowCardinality(String)` | device name at that time |
| `type` | `LowCardinality(String)` | device type at that time |
| `ip` | `IPv4` | device IP at that time |

#### `device`
Latest known state per `device_id` — every scan upserts each seen device.
`ReplacingMergeTree(updated_at)` · primary key `device_id` · query with
`... FROM device FINAL` to collapse to one current row per device.

| Column | Type | Meaning |
|---|---|---|
| `id` | `UUID` | row id (UUIDv7, default) |
| `device_id` | `LowCardinality(String)` | which device (the dedup key) |
| `created_at` | `DateTime` | first seen (default `now()`) |
| `updated_at` | `DateTime` | last upsert — `ReplacingMergeTree` keeps the newest |
| `name` | `LowCardinality(String)` | current name |
| `type` | `LowCardinality(String)` | current type |
| `ip` | `IPv4` | current IP |

### Disk usage

ClickHouse's own `system.*_log` tables (query_log, trace_log, part_log, …) grow
unbounded inside the data volume. [clickhouse/config.d/logging.xml](clickhouse/config.d/logging.xml)
— mounted as a single file into the container — disables them and caps the text
server log. Mount the **file**, not the whole `config.d/` dir, or you shadow the
image's `docker_related_config.xml` and ClickHouse stops listening on the network.

### Migrations

Each migration is a directory `clickhouse/migrations/NNNN_name/` holding `up.sql`
and `down.sql`. Applied migrations are tracked in a `schema_migrations` table the
runner manages itself (it is **not** part of any migration file).

```bash
uv run python main.py migrate up              # apply all pending
uv run python main.py migrate down 1          # roll back the 1 most recent
uv run python main.py migrate status          # [x]/[ ] per migration

# manipulate history WITHOUT running SQL (e.g. baselining an existing DB):
uv run python main.py migrate up --fake        # mark pending as applied
uv run python main.py migrate down 2 --fake    # unmark without dropping anything
```

Each discovered device is `{"name", "model", "device_id", "ip", "type"}`; `name`
is the device nickname (slugged) — rename the plug in the Tapo app and the next
scan picks it up. Devices without on/off (hubs, sensors) are skipped by
`on`/`off`; power readout only shows for energy-monitoring plugs.

## Security notes

- **No CVEs** against the `tapo` library itself (checked 2026-06-07). Known
  TP-Link CVEs (CVE-2025-8065, CVE-2025-14553, etc.) target cameras/app firmware,
  not this Python wrapper.
- **Zero Python transitive deps** for `tapo` — single Rust/pyo3 wheel, small
  supply-chain surface.
- Communication is **local LAN only** (KLAP protocol); no cloud round-trip for
  control. Your TP-Link account creds are used for local auth handshake.
- Credentials live in env vars / gitignored `.env`, never in source.
- Give the plug a **DHCP reservation** so its IP is stable, and keep it on a
  trusted VLAN if possible.

## Star History

<a href="https://www.star-history.com/?repos=nan011%2Ftapo-observability&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=nan011/tapo-observability&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=nan011/tapo-observability&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=nan011/tapo-observability&type=date&legend=top-left" />
 </picture>
</a>

## License

[MIT](LICENSE) © 2026 Nandhika Prayoga
