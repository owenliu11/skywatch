-- 002_compression.sql
-- Kept separate from 001 because these numbers get retuned after measuring,
-- and a new migration is cheaper to reason about than an edited one.

ALTER TABLE state_vectors SET (
    timescaledb.compress,

    -- The one setting to be able to explain out loud: compressed rows are
    -- grouped by aircraft, which is why a per-aircraft track query stays fast
    -- on compressed chunks instead of scanning the whole chunk. This is the
    -- direct answer to "why TimescaleDB rather than plain Postgres".
    timescaledb.compress_segmentby = 'icao24',
    timescaledb.compress_orderby   = 'time DESC'
);


-- 2 days, and the reason is the unique index in 001: inserting into an
-- already-compressed chunk that carries a unique constraint forces a
-- decompress to check it. The writer only ever inserts rows seconds old, so
-- at a 2-day threshold it never touches a compressed chunk.
SELECT add_compression_policy('state_vectors', INTERVAL '2 days');


-- The answer to "what happens after six months".
SELECT add_retention_policy('state_vectors', INTERVAL '30 days');