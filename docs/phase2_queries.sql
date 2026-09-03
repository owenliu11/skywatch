-- Phase 2, Step 5 -- the two query paths, measured.
--
-- Run each with EXPLAIN ANALYZE at your real row count and record the numbers
-- while chunks are uncompressed, then re-run after the compression policy has
-- fired for a before/after. Check the plan says Index Scan (not Seq Scan) and
-- that chunk exclusion touches one or two chunks, not the whole hypertable.

-- 1. Track query. Should hit state_vectors_icao24_time_uniq.
EXPLAIN (ANALYZE, BUFFERS)
SELECT time, latitude, longitude, baro_altitude, velocity, true_track
FROM state_vectors
WHERE icao24 = :'icao24' AND time > now() - INTERVAL '1 hour'
ORDER BY time DESC;

-- 2. Bbox query. Should use the GIST index plus chunk exclusion on time.
EXPLAIN (ANALYZE, BUFFERS)
SELECT DISTINCT ON (icao24) icao24, time, latitude, longitude, velocity
FROM state_vectors
WHERE time > now() - INTERVAL '2 minutes'
  AND position && ST_MakeEnvelope(:lomin, :lamin, :lomax, :lamax, 4326)::geography
ORDER BY icao24, time DESC;

-- Compression ratio, once the policy has fired:
SELECT * FROM hypertable_compression_stats('state_vectors');
-- On TimescaleDB 2.18+ this may be hypertable_columnstore_stats() instead.

-- Manual compression round-trip (run before the policy fires unattended):
--   SELECT compress_chunk(c)   FROM show_chunks('state_vectors') c;
--   SELECT decompress_chunk(c) FROM show_chunks('state_vectors') c;
