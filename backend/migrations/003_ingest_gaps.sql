-- 003_ingest_gaps.sql
-- Written now, used in step 2.
--
-- When the writer's queue overflows and drops records, the next stored vector
-- for that aircraft is further away in both time and space than its
-- neighbours. That is the exact shape the phase 4 teleport feature looks for:
-- our own backpressure can manufacture anomalies.
--
-- Recording every drop lets phase 4 answer "was this a real gap in the sky, or
-- a gap we created". It is the difference between an embarrassing footnote and
-- a measured result.

CREATE TABLE ingest_gaps (
    id            BIGSERIAL PRIMARY KEY,
    region        TEXT        NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    ended_at      TIMESTAMPTZ NOT NULL,
    dropped_count INTEGER     NOT NULL,
    reason        TEXT        NOT NULL   -- 'queue_full' | 'db_unavailable'
);

CREATE INDEX ingest_gaps_window_idx ON ingest_gaps (started_at, ended_at);