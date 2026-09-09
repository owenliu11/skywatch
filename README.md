# SkyWatch

A personal realtime airspace explorer built with FastAPI, React and MapLibre. Phase 3 implements viewport-filtered live aircraft, smooth motion, stale-data indicators, aircraft details, historical tracks and operational metrics. Phase 4 anomaly detection is not implemented.

**Status: Phase 3 local functional acceptance complete.** Full evidence and nonblocking limitations are recorded below.

## Run locally

Set `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET` in the root `.env` (never commit it), then:

```sh
docker compose up -d
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. Backend API documentation is at `http://localhost:8000/docs`; `/health` reports dependency, ingestion, WebSocket and broadcast timing metrics. Stop the API when finished to conserve upstream credits: `docker compose stop api`. Keep the volumes to retain history.

## Phase 3 architecture

OpenSky polling updates the process-local `LiveState` before offering observations to the bounded TimescaleDB write queue. A shared approximately one-second broadcast loop evicts expired aircraft, builds a spatial grid and sends viewport snapshots or changed-aircraft deltas. Unchanged ticks send nothing. Slow consumers drop new frames and request a replacement snapshot; drop and resync counters advance together.

React owns the HUD and selected-aircraft panel; MapLibre owns aircraft rendering. Its 100 ms extrapolation loop awaits the previous source update and skips work when the worker is busy. Extrapolation uses position time, bounded prediction and smoothed server clock offset. Small corrections blend; large corrections snap. Database backpressure can lose history without blocking the live feed. Redis outages interrupt token-dependent polling; existing live state continues serving and ages out normally. Cold startup requires both dependencies.

The frontend reconnects with exponential backoff and equal jitter (initial delay 0.5–1 s; capped at 15–30 s). A valid aircraft frame resets backoff. Disconnect/error clears aircraft and selection through the existing serialized source-update path and stops extrapolation; reconnection sends the current viewport for a fresh snapshot. Cleanup cancels pending retries.

## Staleness policy

The earlier Phase 3 measurement was **p50 = 9 s, p90 = 17 s, p99 = 29 s**. These are preserved results from the build conversation, not a newly measured distribution. The original raw output and maximum gap were not available in this pass.

- `STALE_AFTER_S = 20`: stop extrapolation and fade aircraft at 20 seconds since `last_contact`.
- `LOST_AFTER_S = 60`: backend expiry removes aircraft older than the 60-second cutoff on its next tick.

Twenty seconds rounds above the measured p90; sixty seconds deliberately leaves margin beyond p99 for polling delays. The guide's SQL measures inter-observation gaps, which inform this policy but are not the same as instantaneous position age:

```sql
SELECT percentile_disc(0.50) WITHIN GROUP (ORDER BY d) AS p50,
       percentile_disc(0.90) WITHIN GROUP (ORDER BY d) AS p90,
       percentile_disc(0.99) WITHIN GROUP (ORDER BY d) AS p99,
       max(d) AS max_gap
FROM (
  SELECT EXTRACT(EPOCH FROM time - LAG(time) OVER
    (PARTITION BY icao24 ORDER BY time)) AS d
  FROM state_vectors
  WHERE time > now() - INTERVAL '2 hours'
) s
WHERE d IS NOT NULL AND d > 0;
```

## API and WebSocket Origin configuration

`GET /anomalies?since=&type=` always returns HTTP 200 with `[]`. Both optional query parameters are reserved, ignored string filters, documented in OpenAPI. This is a Phase 4 placeholder, not evidence that an anomaly scan found nothing.

`WS_ALLOWED_ORIGINS` is a comma-separated list of exact browser origins, passed through Compose. Defaults allow `http://localhost:5173`, `http://127.0.0.1:5173`, `http://localhost:8000` and `http://127.0.0.1:8000`. Set it in the root `.env` and recreate the API (`docker compose up -d api`) when using another frontend origin. Present but unlisted origins, including `null`, are rejected before acceptance. Origin-less CLI clients are supported; Origin validation is not authentication. Uvicorn's protocol ping interval and timeout remain 20 seconds.

## Validation and practical performance

See [Phase 3 completion evidence](PHASE3_COMPLETION.md) for the file inventory, command log, failure drills, performance measurements and remaining limits. The earlier [Step 7 report](PHASE3_STEP7_ACCEPTANCE.md) is retained as historical evidence.

At approximately 100 real aircraft, the 30-second WebSocket check received only four frames in a fixed viewport. Zoom reduced snapshot size from 9,209 to 2,271 bytes. Backend tick last/p95 was 0.794 / 2.018 ms. Browser source-update p95 was approximately 3.5–4.1 ms in sampled windows, within the 100 ms animation interval. These are local smoke measurements, not a load-test guarantee.

For a repeatable lightweight browser measurement, open `http://localhost:5173/?perf=1` and inspect console messages named `SkyWatch render performance`. Every 100 completed updates reports aircraft count, elapsed window time, p50/p95/max update time and skipped busy calls. Timing includes feature construction and the awaited MapLibre source/worker update; it does not measure final GPU presentation. Normal URLs do not collect timing samples.

```sh
docker compose exec -T api ruff check .
docker compose exec -T api pytest -v
docker compose exec -T api mypy --cache-dir=/tmp/skywatch-mypy-cache app
cd frontend
npm run build
npm run lint
```

Backend tests use real TimescaleDB with transaction rollback. Mypy is currently nonblocking in CI and reports existing ingestion typing errors; see the completion report.
