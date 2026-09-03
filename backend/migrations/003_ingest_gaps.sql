-- Backpressure is drop-newest (see app/ingest/writer.py). A dropped run of
-- records leaves the next stored vector further away in time and space than its
-- neighbours -- the exact shape jump_distance_per_s looks for. Recording the
-- drops lets Phase 4 join against this table and answer "was this a real gap in
-- the sky, or a gap we created?"
CREATE TABLE ingest_gaps (
    id            BIGSERIAL PRIMARY KEY,
    region        TEXT        NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    ended_at      TIMESTAMPTZ NOT NULL,
    dropped_count INTEGER     NOT NULL,
    reason        TEXT        NOT NULL   -- 'queue_full' | 'db_unavailable'
);

CREATE INDEX ingest_gaps_started_at_idx ON ingest_gaps (started_at DESC);
