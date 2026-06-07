"""ClickHouse integration: connection, migration runner, and insert helpers.

Connection comes from the environment (see .env.example). The migration runner
tracks applied migrations in a `schema_migrations` table it manages itself — that
table is NOT part of any migration file. Migrations live in ./migrations as pairs
of NNNN_name.up.sql / NNNN_name.down.sql.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import clickhouse_connect
from clickhouse_connect.driver.client import Client
from dotenv import load_dotenv

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATIONS_TABLE = "schema_migrations"

# each migration is a folder: migrations/NNNN_name/{up.sql,down.sql}
_VERSION_RE = re.compile(r"^(\d+)_(.+)$")


# --- connection -----------------------------------------------------------


def ch_configured() -> bool:
    load_dotenv()
    return bool(os.getenv("CLICKHOUSE_HOST"))


def get_client() -> Client:
    """Build a ClickHouse client from env. Raises SystemExit if unconfigured."""
    load_dotenv()
    host = os.getenv("CLICKHOUSE_HOST")
    if not host:
        raise SystemExit("ClickHouse not configured — set CLICKHOUSE_HOST etc (see .env.example)")
    return clickhouse_connect.get_client(
        host=host,
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "default"),
        secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() in ("1", "true", "yes"),
    )


# --- migration discovery --------------------------------------------------


def _discover_migrations() -> list[dict]:
    """Return sorted [{version, name, up, down}] from the migrations directory.

    Each migration is a folder `NNNN_name/` holding `up.sql` and `down.sql`.
    """
    out: list[dict] = []
    if not MIGRATIONS_DIR.exists():
        return out
    for d in sorted(p for p in MIGRATIONS_DIR.iterdir() if p.is_dir()):
        m = _VERSION_RE.match(d.name)
        if not m:
            continue
        version, name = m.group(1), m.group(2)
        out.append({
            "version": version,
            "name": name,
            "up": d / "up.sql",
            "down": d / "down.sql",
        })
    out.sort(key=lambda x: x["version"])
    return out


def _split_statements(sql: str) -> list[str]:
    """Split a SQL file into individual statements (ClickHouse runs one at a time).

    Full-line `--` comments are stripped first, so a semicolon inside a comment
    doesn't get mistaken for a statement separator.
    """
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    cleaned = "\n".join(lines)
    return [s.strip() for s in cleaned.split(";") if s.strip()]


def _run_sql_file(client: Client, path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Missing SQL file: {path.name}")
    for stmt in _split_statements(path.read_text()):
        client.command(stmt)


# --- migration tracking table (managed here, not in migration files) ------


def _ensure_table(client: Client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE}
        (
            version    String,
            name       String,
            applied_at DateTime64(3) DEFAULT now64(3)
        )
        ENGINE = MergeTree
        ORDER BY version
        """
    )


def _applied_versions(client: Client) -> set[str]:
    rows = client.query(f"SELECT version FROM {MIGRATIONS_TABLE}").result_rows
    return {r[0] for r in rows}


def _record(client: Client, version: str, name: str) -> None:
    client.insert(
        MIGRATIONS_TABLE,
        [[version, name, datetime.now(timezone.utc)]],
        column_names=["version", "name", "applied_at"],
    )


def _unrecord(client: Client, version: str) -> None:
    client.command(f"DELETE FROM {MIGRATIONS_TABLE} WHERE version = '{version}'")


# --- migrate up / down ----------------------------------------------------


def migrate_up(fake: bool = False) -> None:
    """Apply all pending migrations. With fake=True, only record them as applied."""
    client = get_client()
    _ensure_table(client)
    applied = _applied_versions(client)
    pending = [m for m in _discover_migrations() if m["version"] not in applied]
    if not pending:
        print("Up to date — no pending migrations.")
        return
    for m in pending:
        tag = m["version"] + "_" + m["name"]
        if fake:
            print(f"[fake] mark applied: {tag}")
        else:
            print(f"applying: {tag}")
            _run_sql_file(client, m["up"])
        _record(client, m["version"], m["name"])
    print(f"{'Recorded' if fake else 'Applied'} {len(pending)} migration(s).")


def migrate_down(steps: int, fake: bool = False) -> None:
    """Roll back the `steps` most recently applied migrations.

    With fake=True, only remove them from history (don't run down SQL).
    """
    if steps < 1:
        raise SystemExit("down requires a positive number of steps")
    client = get_client()
    _ensure_table(client)
    applied = _applied_versions(client)
    known = {m["version"]: m for m in _discover_migrations()}
    # most-recent first
    targets = sorted(applied, reverse=True)[:steps]
    if not targets:
        print("Nothing to roll back.")
        return
    for version in targets:
        m = known.get(version)
        name = m["name"] if m else "?"
        tag = f"{version}_{name}"
        if fake:
            print(f"[fake] unmark: {tag}")
        else:
            if m is None:
                raise SystemExit(f"No down file for applied version {version}; use --fake to force.")
            print(f"reverting: {tag}")
            _run_sql_file(client, m["down"])
        _unrecord(client, version)
    print(f"{'Unrecorded' if fake else 'Reverted'} {len(targets)} migration(s).")


def migration_status() -> None:
    client = get_client()
    _ensure_table(client)
    applied = _applied_versions(client)
    for m in _discover_migrations():
        mark = "x" if m["version"] in applied else " "
        print(f"  [{mark}] {m['version']}_{m['name']}")


# --- data inserts ---------------------------------------------------------


def insert_power(
    client: Client,
    *,
    device_id: str,
    power_used: float,
    power_used_at: datetime,
) -> None:
    """Insert one power reading. id and created_at are filled by the DB defaults."""
    client.insert(
        "device_power_usage",
        [[device_id, float(power_used), power_used_at]],
        column_names=["device_id", "power_used", "power_used_at"],
    )


def insert_snapshot(
    client: Client, *, device_id: str, name: str, type: str, ip: str
) -> None:
    """Append a device_snapshot row capturing the current name/type/ip."""
    client.insert(
        "device_snapshot",
        [[device_id, name, type, ip]],
        column_names=["device_id", "name", "type", "ip"],
    )


def upsert_device(
    client: Client, *, device_id: str, name: str, type: str, ip: str
) -> None:
    """Write the latest state for a device_id into `device`.

    ReplacingMergeTree(updated_at) collapses to the newest row, so inserting with
    a fresh updated_at on every scan keeps one current row per device_id.
    """
    client.insert(
        "device",
        [[device_id, name, type, ip, datetime.now(timezone.utc)]],
        column_names=["device_id", "name", "type", "ip", "updated_at"],
    )
