CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;

-- One row per aircraft ever seen. Slowly changing; upserted every batch.
CREATE TABLE aircraft (
    icao24          TEXT PRIMARY KEY,
    callsign        TEXT,           -- most recent NON-NULL callsign
    origin_country  TEXT,
    category        SMALLINT,       -- NULL unless the client sends extended=1
    first_seen      TIMESTAMPTZ NOT NULL,
    last_seen       TIMESTAMPTZ NOT NULL
);

CREATE TABLE state_vectors (
    -- Partition column: OpenSky's last_contact. Always present, never NULL, and
    -- it advances only when the transponder was actually heard -- which is what
    -- makes the derived update-interval features mean something.
    time            TIMESTAMPTZ      NOT NULL,
    icao24          TEXT             NOT NULL,

    -- When the POSITION was last valid. NULL is a real and common value: an
    -- aircraft heard by a receiver but reporting no position.
    time_position   TIMESTAMPTZ,

    -- Source of truth for coordinates. DOUBLE PRECISION, not REAL: float32
    -- rounds latitude to roughly a metre, which is inside the noise band the
    -- teleport detector operates in.
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,

    -- Derived, purely so a GIST index exists for bbox queries. ST_MakePoint is
    -- STRICT, so a NULL coordinate yields a NULL point, not (0,0). That is the
    -- same no-fabricated-zeros rule as the parser, enforced by the database.
    position        GEOGRAPHY(POINT, 4326)
                    GENERATED ALWAYS AS (
                        ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
                    ) STORED,

    -- SI units exactly as the API delivers them: metres, metres/second, degrees.
    -- Conversion to knots and fpm happens once, in detect/features.py.
    baro_altitude   REAL,
    geo_altitude    REAL,
    velocity        REAL,
    true_track      REAL,
    vertical_rate   REAL,

    on_ground       BOOLEAN          NOT NULL,
    squawk          TEXT,
    spi             BOOLEAN          NOT NULL,
    position_source SMALLINT         NOT NULL,

    -- Provenance.
    region          TEXT             NOT NULL,
    ingested_at     TIMESTAMPTZ      NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'state_vectors', 'time',
    chunk_time_interval    => INTERVAL '1 day',
    create_default_indexes => FALSE      -- declared explicitly below
);

-- Dedup key AND the per-aircraft track index in one object.
-- Ascending, not DESC: ON CONFLICT arbiter inference is easier to reason about,
-- and Postgres scans a two-column btree backwards at the same cost, so the
-- track query (icao24 = $1 ORDER BY time DESC) loses nothing.
CREATE UNIQUE INDEX state_vectors_icao24_time_uniq
    ON state_vectors (icao24, time);

CREATE INDEX state_vectors_time_idx     ON state_vectors (time DESC);
CREATE INDEX state_vectors_position_gix ON state_vectors USING GIST (position);
