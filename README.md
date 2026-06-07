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

## What it can do

Observability is the headline, but the same CLI (`main.py`) is a full Tapo
manager. Entry points:

| Command | What it does |
|---|---|
| `monitor` | **Observability** — sample power, write the per-interval mean to ClickHouse (`device_power_usage`). One async task per device, self-healing. |
| `discover` | Find Tapo devices on the LAN (UDP broadcast); `--save` writes the registry and upserts `device` / `device_snapshot` in ClickHouse. |
| `list` | Show registered devices (name, model, ip, device_id). |
| `status` | Live state + current power for a device or `all`. |
| `on` / `off` | **Control** — switch a plug (or `all`) on/off. |
| `migrate up/down/status` | ClickHouse schema migrations (`--fake` manages history only). |

Devices live in a registry (`./local/devices.json`: name + model + device_id + ip + type),
built by `discover`. Any command targets a device by name, `device_id` prefix
(git short-SHA style), or `all`. `.env` holds only your TP-Link account
email/password — gitignored, never commit it.

## Getting Started

Brings up ClickHouse + this service on a Docker **bridge** network. The app
reaches ClickHouse by service name and reaches Tapo devices by unicast IP (the
host NATs the container out to your LAN), so `monitor` works without host
networking.

**Discovery is a host step.** UDP broadcast can't cross a bridge, so you run
discovery on the host once — it writes `./local/devices.json` (mounted into the
container) and, with ClickHouse up, the `device` / `device_snapshot` tables.

```bash
cp .env.example .env                              # set TAPO creds + MONITOR_* params
cp docker-compose.yml.example docker-compose.yml  # one-time; edit freely (gitignored)

uv sync                                           # host deps
uv run python main.py discover --save             # populate ./local/devices.json (+ CH if up)

sh scripts/start.sh                               # build, down, up --build, follow logs
```

The container runs migrations, waits for `./local/devices.json` if it isn't
there yet, then starts `monitor` (mean power per interval into
`device_power_usage`). Re-run the host `discover --save` whenever devices change
(new device, renamed, new IP).

`docker-compose.yml` is gitignored so your local edits stay out of version
control — copy it from the committed `docker-compose.yml.example` once, before
the first `start.sh`.

Configure the run via `.env` (the service's `monitor` takes two parameters):

- `MONITOR_DEVICES` — which devices: name(s)/id prefix(es), space-separated, or `all`
- `MONITOR_INTERVAL` — window seconds; one **mean** row per window per device
- `MONITOR_SAMPLE` — seconds between samples inside the window

> Want discovery to run *inside* the container instead? Switch the service to
> `network_mode: host` in `docker-compose.yml` and set `RUN_DISCOVERY=true` —
> then it discovers on startup (Linux only; reach ClickHouse via `127.0.0.1`).

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


## Development Setup

```bash
uv sync                 # install deps (tapo, python-dotenv)
cp .env.example .env    # then edit .env with your TP-Link creds
```

## Usage

`scripts/main.sh` is a gateway to the `main.py` CLI **inside the running app
container** — it `docker exec`s into `tapo-observability` and forwards every arg:

```bash
sh ./scripts/main.sh list
sh ./scripts/main.sh status all
sh ./scripts/main.sh migrate up
```

Most commands work this way. **Exception:** `discover` needs LAN broadcast,
which the bridge-networked container can't do — run it on the **host**:
`uv run python main.py discover --save`.

```bash
# 1. find devices and save the registry (auto-detects your /24 subnet)
uv run python main.py discover --save
uv run python main.py discover --target 192.168.1.255 --save   # or pass broadcast explicitly

# 2. inspect / control by name (or 'all')
uv run python main.py list             # show registered devices
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
created by the first migration (column types picked for minimal size):

- **device_power_usage** — one row per power reading written by `monitor`:
  `device_id` (LowCardinality(String)), `power_used` (Decimal32(3) watts —
  the **mean** over the window), `power_used_at` (DateTime, window close time),
  `window_seconds` (UInt16 — measured seconds since the previous row's
  `power_used_at`), `created_at` (DateTime, DB insert time). `MergeTree`,
  partitioned monthly by `toYYYYMM(power_used_at)`, primary key
  `(device_id, power_used_at)`.
  Energy: `kWh = power_used * window_seconds / 3600 / 1000`.
- **device_snapshot** — append-only metadata history: `id` (UUIDv7), `device_id`
  (LowCardinality(String)), `created_at` (DateTime), `name`/`type`
  (LowCardinality(String)), `ip` (IPv4). Primary key `(device_id, created_at)`.
  A new row is written by `discover --save` whenever a device's name/type/ip
  changes (and once when first seen).
- **device** — latest known state per device_id: same columns as device_snapshot
  plus `updated_at` (DateTime). `ReplacingMergeTree(updated_at)`, primary key
  `device_id` — query with `... FROM device FINAL` to collapse to one current row
  per device. `discover --save` upserts every seen device here.

### Disk usage

ClickHouse's own `system.*_log` tables (query_log, trace_log, part_log, …) grow
unbounded inside the data volume. [clickhouse/config.d/logging.xml](clickhouse/config.d/logging.xml)
— mounted as a single file into the container — disables them and caps the text
server log. Mount the **file**, not the whole `config.d/` dir, or you shadow the
image's `docker_related_config.xml` and ClickHouse stops listening on the network.

### Migrations

Each migration is a directory `migrations/NNNN_name/` holding `up.sql` and
`down.sql`. Applied migrations are tracked in a `schema_migrations` table the
runner manages itself (it is **not** part of any migration file).

```bash
uv run python main.py migrate up              # apply all pending
uv run python main.py migrate down 1          # roll back the 1 most recent
uv run python main.py migrate status          # [x]/[ ] per migration

# manipulate history WITHOUT running SQL (e.g. baselining an existing DB):
uv run python main.py migrate up --fake        # mark pending as applied
uv run python main.py migrate down 2 --fake    # unmark without dropping anything
```

Each registry entry is `{"name", "model", "device_id", "ip", "type"}`; `name`
defaults to the device nickname (slugged). Edit `./local/devices.json` to rename.
Devices without on/off (hubs, sensors) are skipped by `on`/`off`; power readout
only shows for energy-monitoring plugs.

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

## License

[MIT](LICENSE) © 2026 Nandhika Prayoga
