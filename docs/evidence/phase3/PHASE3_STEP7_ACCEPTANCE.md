# Phase 3 Step 7 acceptance report

> Follow-up: [Phase 3 completion report](PHASE3_COMPLETION.md) supersedes the remaining acceptance items below. This original Step 7 evidence is retained unchanged.

Verified September 8, 2026 against the existing Desktop SkyWatch architecture and the Phase 3 v2 guide. Existing uncommitted Phase 3 work was preserved. No frontend, region configuration, Compose, schema, or synced guide files were edited in this pass.

## Changes in this pass

| File | Change |
| --- | --- |
| `backend/app/state.py` | Process-lifetime eviction counter; only actual removals count. |
| `backend/app/api/ws.py` | Successful aircraft-frame sends, queue-full drops, drop-triggered resync requests, broadcast work timing, rolling nearest-rank p95. |
| `backend/app/main.py` | Expose all nine realtime health fields alongside existing queue/credit metrics; correct scheduler startup log to count enabled regions. |
| `backend/app/ingest/writer.py` | Recover from database connection/write failures: account failed history batches as `db_unavailable` drops, preserve gap records for the next successful batch, acknowledge dropped queue items, and continue after a one-second backoff. |
| `backend/app/ingest/opensky.py` | Convert Redis errors into failed fetch results so the scheduler follows its normal retry path. |
| `backend/tests/test_realtime_metrics.py` | Nine cases covering all viewport edges, eviction boundaries/counters, rolling p95, actual broadcast drop/resync/idle behavior, successful-send accounting, and WebSocket connection/health lifecycle. |
| `backend/tests/test_dependency_recovery.py` | Two deterministic failure/recovery tests for database writer accounting and Redis fetch retry. |
| `PHASE3_STEP7_ACCEPTANCE.md` | This report and remaining acceptance procedure. |

## Metric definitions

- `live_map_size`: current process's in-memory aircraft count.
- `live_map_evicted_total`: actual TTL removals since this LiveState was created; repeating eviction does not count an aircraft twice.
- `ws_connections`: active registered connections; `ws_connections_max`: configured capacity (200), not a high-water mark.
- `ws_frames_sent_total`: successfully completed aircraft snapshot/delta sends, excluding protocol error responses. Enqueuing or a failed send does not count.
- `ws_frames_dropped_total`: rejected outbound frames when the bounded queue is full.
- `ws_resyncs_total`: drop-triggered resync requests, not successfully delivered snapshots. Each rejected frame increments both counters exactly once, including repeated full-queue attempts. Initial connections and viewport changes do not count as resyncs.
- `tick_ms_last`: elapsed broadcast work using a monotonic timer, including eviction, grid construction, filtering and frame offers; excludes the one-second sleep and socket I/O.
- `tick_ms_p95`: nearest-rank p95 of the last 300 completed ticks (approximately five minutes at idle), zero before the first tick.
- Counters are in memory and process-local; a process restart resets them.

## Test audit

The existing tests already cover newer/duplicate/older live-state handling, TTL removal, REST bbox filtering/null positions/area cap/antimeridian rejection, track 24-hour clamping and truncation against real TimescaleDB, delta enter/update/leave, unchanged-frame suppression, viewport snapshot reset, and slow-consumer resync. Missing edge and metric integration coverage was added without duplicating those tests.

Guide clarification: an explicit viewport message forces a snapshot. A previously visible aircraft leaving the current viewport produces a delta `leave`; a pan is represented by a replacement snapshot. These are consistent with the existing protocol and frontend.

Commands run from the project directory:

```sh
docker info --format '{{.ServerVersion}}'
docker compose ps
docker compose up -d db redis
docker compose run --rm --no-deps api sh -c 'ruff check . && pytest -q'
# Import-only lint fixes were applied to the touched files using ruff check --fix.
cd frontend
npm run build
cd ..
docker compose up -d api
# A short Python/httpx/websockets script ran via docker compose exec -T api.
docker compose logs --tail 25 api
docker compose stop api
# Effective settings verified inside the API container: bay_area, socal.
docker compose stop db redis
```

Final backend result: **ruff passed; 96 tests passed**. Database tests used real TimescaleDB with transaction rollback. Nonblocking warnings: Starlette/httpx and AnyIO deprecations, existing TestPool helper collection warning, and a Docker-mounted pytest cache write warning. Frontend production build passed; Vite reported a large bundle warning. No dependency upgrades or bundle redesign were made.

## Real runtime evidence

Normal app lifespan started cleanly; all existing migrations were already applied. Effective polling regions remained `bay_area,socal`. Actual upstream ingestion and database persistence worked.

| Check | Observed |
| --- | --- |
| `/health` | HTTP 200, status `ok`, database and Redis true |
| Live map | 109 before connection; 110 after check |
| Evictions | 17 before; 26 after |
| Connections | 0 → 1 → 0 |
| Successful WS frames | 0 → 1; received snapshot with 28 aircraft |
| WS drops/resyncs | Both 0 throughout |
| Stationary viewport | No additional frames in the next four seconds |
| Final tick last/p95 | Approximately 0.466 ms / 0.909 ms |
| Database queue | Depth 0, capacity 10,000, drops 0 |
| Rows inserted | 151 before; 228 after |
| Credits | 2,314 before probe; 2,313 after (startup polling occurred before probe) |

These are short smoke-check measurements, not load-test or long-monitoring results. API, database and Redis containers started for this pass were stopped afterward; persisted volumes were retained. Docker Desktop itself was already running and was left running.

The frontend source was inspected and left unchanged: smooth reconciliation/dead reckoning, heading rotation, altitude colors, 20-second stale threshold, 60-second backend expiry, detail panel, historical track and operational HUD remain present. Build compatibility passed; visual behavior was not newly certified in a browser.

## Dependency failure behavior and remaining manual acceptance

Deterministic tests inject a failed database acquire, then recovery, and confirm the writer remains alive, lost rows are counted, gap records reach the next write, and the queue drains. A Redis failure then successful fetch confirms the fetch path recovers. The health/WebSocket test confirms the live endpoint still accepts connections and health returns `degraded` when dependency probes fail.

Redis remains required for OAuth token retrieval. During an outage, polling retries with the existing backoff; live endpoints continue serving in-memory data, which ages out normally. After Redis returns, token retrieval/cache population resumes. This pass does not add an in-process OAuth fallback. Cold startup still requires both dependencies. A DB outage makes historical-track requests unavailable; existing live state and broadcast work are independent of the writer.

Real container interruption/restart was not exercised. To finish that acceptance on a quiet local session:

1. Run `docker compose up -d`, then `cd frontend && npm run dev`. Open the displayed frontend URL; keep `/ws/live` open in browser developer tools and record `/health`.
2. Run `docker compose stop db` while leaving API/Redis running. Confirm the WebSocket remains connected and live observations continue. Allow at least one write batch to fail; verify `dropped_total` increases and `/health` becomes degraded. Health probing may wait for the existing DB connection timeout.
3. Run `docker compose start db`. Confirm `/health` returns to `ok`, `rows_inserted_total` advances again, and `ingest_gaps` contains `db_unavailable`. Do not remove volumes or delete data.
4. Run `docker compose stop redis`. Confirm REST live aircraft and WebSocket service remain available, `/health` reports Redis false, and polling failures back off. Aircraft may become stale/lost while upstream polling is unavailable.
5. Run `docker compose start redis`. Confirm Redis health, token-cache refill and polling recovery after the scheduler's retry interval. Do not print token values.
6. Stop API polling when finished to conserve credits. Restore dependencies even if a check fails.

Remaining browser/documentation acceptance from the broader Phase 3 guide:

- Visually check heading, motion, altitude colors, stale fading, detail/track/HUD, and smaller filtered payloads on zoom/pan.
- Measure and save frontend frame cost at a representative real aircraft count; backend tick timings do not substitute for browser frame timing.
- Preserve the raw staleness query output alongside the README. Existing source comments record p50=9s, p90=17s, p99=29s and the chosen 20s/60s thresholds; this pass did not reproduce the historical measurement.
- Record the demo GIF if desired.

Step 7 implementation, test audit, lint/build and short runtime checks are complete. Full Phase 3 visual/performance sign-off and real dependency restart acceptance remain as listed above.
