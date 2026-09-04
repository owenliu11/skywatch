CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;


CREATE TABLE aircraft (
    icao24          TEXT PRIMARY KEY,
    callsign        TEXT,
    origin_country  TEXT,
    category        SMALLINT,
    first_seen      TIMESTAMPTZ NOT NULL,
    last_seen       TIMESTAMPTZ NOT NULL
);


CREATE TABLE state_vectors (
    time            TIMESTAMPTZ NOT NULL,
    icao24          TEXT NOT NULL,

    time_position   TIMESTAMPTZ,

    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,

    position        GEOGRAPHY(POINT, 4326)
                    GENERATED ALWAYS AS (
                        ST_SetSRID(
                            ST_MakePoint(longitude, latitude),
                            4326
                        )::geography
                    ) STORED,

    baro_altitude   REAL,
    geo_altitude    REAL,
    velocity        REAL,
    true_track      REAL,
    vertical_rate   REAL,

    on_ground       BOOLEAN NOT NULL,
    squawk          TEXT,
    spi             BOOLEAN NOT NULL,
    position_source SMALLINT NOT NULL,

    region          TEXT NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);


SELECT create_hypertable(
    'state_vectors',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    create_default_indexes => FALSE
);


-- Dedup key AND the per-aircraft track index, in one object.
--
-- Polling every 5-10s against transponders that report every 10-60s means
-- most rows offered are repeats of a state already stored. ON CONFLICT
-- against this index discards them at insert time. Without it, the derived
-- update_interval_s feature would measure the poll rate rather than the
-- aircraft's report rate.
--
-- Ascending, not DESC: ON CONFLICT arbiter inference is simpler to reason
-- about against an ascending index, and Postgres scans a two-column btree
-- backwards at the same cost, so ORDER BY time DESC loses nothing.
--
-- Note also: every partitioning dimension must appear in a unique index on a
-- hypertable. `time` is the only dimension, so this is legal.

CREATE UNIQUE INDEX state_vectors_icao24_time_uniq
    ON state_vectors (icao24, time);


CREATE INDEX state_vectors_time_idx
    ON state_vectors (time DESC);


CREATE INDEX state_vectors_position_gix
    ON state_vectors USING GIST (position);