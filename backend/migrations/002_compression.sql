-- Separate file, because you will retune it after measuring.

ALTER TABLE state_vectors SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'icao24',
    timescaledb.compress_orderby   = 'time DESC'
);

-- 2 days, and the reason is the unique index in 001: inserting into an already
-- compressed chunk with a unique constraint forces a decompress to check it.
-- The writer only ever inserts rows seconds old, so at 2 days it never touches
-- a compressed chunk.
SELECT add_compression_policy('state_vectors', INTERVAL '2 days');

SELECT add_retention_policy('state_vectors', INTERVAL '30 days');
