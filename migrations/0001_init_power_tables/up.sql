-- device_power_usage: one row per power reading emitted by `monitor`.
-- id and created_at are filled by the DB; name/ip/type are point-in-time snapshots.
CREATE TABLE IF NOT EXISTS device_power_usage
(
    id            UUID         DEFAULT generateUUIDv7(),
    device_id     String,
    power_used    Float64,
    power_used_at DateTime64(3),
    created_at    DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(power_used_at)
PRIMARY KEY (device_id, power_used_at)
ORDER BY (device_id, power_used_at);

-- device_snapshot: append-only history; a new row is written whenever a device's
-- name/type/ip changes (and once when first seen).
CREATE TABLE IF NOT EXISTS device_snapshot
(
    id         UUID          DEFAULT generateUUIDv7(),
    device_id  String,
    created_at DateTime64(3) DEFAULT now64(3),
    name       String,
    type       String,
    ip         String
)
ENGINE = MergeTree
PRIMARY KEY (device_id, created_at)
ORDER BY (device_id, created_at);

-- device: latest known state per device_id. ReplacingMergeTree collapses to the
-- row with the greatest updated_at, so each device_id keeps only its newest row.
CREATE TABLE IF NOT EXISTS device
(
    id         UUID          DEFAULT generateUUIDv7(),
    device_id  String,
    created_at DateTime64(3) DEFAULT now64(3),
    updated_at DateTime64(3) DEFAULT now64(3),
    name       String,
    type       String,
    ip         String
)
ENGINE = ReplacingMergeTree(updated_at)
PRIMARY KEY device_id
ORDER BY device_id;
