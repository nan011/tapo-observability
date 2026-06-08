-- device_power_usage: one row per power reading emitted by `monitor`.
-- power_used is the MEAN watts over the `window_seconds` ending at power_used_at,
-- so energy = power_used * window_seconds / 3600 / 1000 kWh. created_at is the
-- DB insert time, filled by default.
CREATE TABLE IF NOT EXISTS device_power_usage
(
    device_id      LowCardinality(String),
    power_used     Decimal32(3),
    power_used_at  DateTime,
    window_seconds Decimal32(3),
    created_at     DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(power_used_at)
PRIMARY KEY (device_id, power_used_at)
ORDER BY (device_id, power_used_at);

-- device_snapshot: append-only history; a new row is written whenever a device's
-- name/type/ip changes (and once when first seen).
CREATE TABLE IF NOT EXISTS device_snapshot
(
    id         UUID DEFAULT generateUUIDv7(),
    device_id  LowCardinality(String),
    created_at DateTime DEFAULT now(),
    name       LowCardinality(String),
    type       LowCardinality(String),
    ip         IPv4
)
ENGINE = MergeTree
PRIMARY KEY (device_id, created_at)
ORDER BY (device_id, created_at);

-- device: latest known state per device_id. ReplacingMergeTree collapses to the
-- row with the greatest updated_at, so each device_id keeps only its newest row.
CREATE TABLE IF NOT EXISTS device
(
    id         UUID DEFAULT generateUUIDv7(),
    device_id  LowCardinality(String),
    created_at DateTime DEFAULT now(),
    updated_at DateTime DEFAULT now(),
    name       LowCardinality(String),
    type       LowCardinality(String),
    ip         IPv4
)
ENGINE = ReplacingMergeTree(updated_at)
PRIMARY KEY device_id
ORDER BY device_id;
