CREATE TABLE IF NOT EXISTS anomalies (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    icao24 TEXT NOT NULL,
    detector TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 1,
    score DOUBLE PRECISION,
    features JSONB NOT NULL,
    region TEXT NOT NULL,
    is_synthetic BOOLEAN NOT NULL DEFAULT FALSE
);
ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS hit_count INTEGER NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS anomalies_detected_at_idx ON anomalies (detected_at DESC);
CREATE INDEX IF NOT EXISTS anomalies_icao24_idx ON anomalies (icao24, detected_at DESC);
CREATE INDEX IF NOT EXISTS anomalies_window_start_idx ON anomalies (window_start DESC);
