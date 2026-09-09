# Phase 3 completion report

Verified September 8, 2026 local time (September 9 UTC), against the existing Desktop repo and the read-only Phase 3 v2 guide. This report supersedes the remaining-acceptance list in `PHASE3_STEP7_ACCEPTANCE.md`. Existing uncommitted Phase 3 and Step 7 work was preserved. No Phase 4 detection, schema changes, dependency upgrades or UI redesign were made.

## Files changed in this completion pass

| File | Change |
| --- | --- |
| `backend/app/api/anomalies.py` | New documented GET stub with optional ignored `since` and `type` filters; always `[]`. |
| `backend/app/main.py` | Register the anomalies router. Existing Step 7 metrics retained. |
| `backend/app/config.py` | Small comma-separated exact Origin allowlist setting. |
| `backend/app/api/ws.py` | Reject unlisted browser Origins before accepting the socket. Preserve origin-less CLI support. |
| `docker-compose.yml` | Forward `WS_ALLOWED_ORIGINS`, with explicit local defaults. |
| `backend/tests/test_phase3_completion.py` | Eight parameterized cases for stub queries, rejected origins, configured allowed origin and missing Origin. |
| `frontend/src/App.tsx` | Exponential retry with capped equal jitter, disconnect clearing through serialized MapLibre updates, cleanup cancellation, automatic retry messaging, and opt-in `?perf=1` timing. Existing viewport-on-open behavior retained. |
| `README.md` | Architecture, startup, API/config contract, staleness evidence and policy, measurement instructions and validation status. |
| `PHASE3_STEP7_ACCEPTANCE.md` | Link to this superseding completion report. |
| `PHASE3_COMPLETION.md` | This evidence and command/check inventory. |

The repo was already dirty, with much of Phase 3 including the frontend untracked. Therefore a plain `git diff --stat` does not describe this pass alone. No existing edits were reverted and no commit was created. Generated frontend build output is not a source change.

## Runtime acceptance

The API, Postgres and Redis were already running at entry. Dependencies were restored after each drill; volumes were never removed. The original running service state was restored at completion.

### Payloads and stationary viewport

A small Python/httpx/websockets probe ran in the existing API container using real upstream observations and an allowed browser Origin.

| Measurement | Result |
| --- | --- |
| Wide bbox `[32,-123,39,-116]` | Snapshot: 99 aircraft, 9,209 UTF-8 bytes |
| Zoom bbox `[37.2,-122.6,38.1,-121.7]` | Snapshot: 23 aircraft, 2,271 bytes (75.3% smaller) |
| Fixed zoom viewport for 30 s after initial snapshot | Four frames: 95, 2,160, 94, 2,245 bytes; roughly 26/30 one-second opportunities silent |
| End live map count | 99 |
| End tick last / rolling p95 | 0.794 / 2.018 ms |
| WS dropped / resync counters | 0 / 0 |

Snapshots were requested sequentially, so counts can vary slightly with real traffic. Backend tick time excludes socket I/O and sleep. The stationary test verifies actual suppression, not a synthetic empty viewport.

### Postgres failure and recovery

A persistent WebSocket and HTTP observer sampled every five seconds while `db` was stopped for approximately 25 seconds. The same socket remained alive throughout both drills.

- Before: status `ok`, 99 live aircraft, 711 inserted rows, zero dropped rows.
- During DB outage: `/health` returned degraded with database false; `/aircraft` continued returning HTTP 200. WebSocket updates continued (91 enter/update observations by 05:23:07 UTC), with about 96–100 live aircraft.
- Lost history was accounted for: `dropped_total` reached **153**, while queue depth stayed zero.
- After restart: database health recovered by 05:23:22; inserted rows advanced to 776 and then 807. The existing writer resumed without restarting the API.
- Read-only SQL confirmed persisted `db_unavailable` gaps totaling **73 Bay Area + 80 SoCal = 153** rows in this drill's time window.

### Redis failure and recovery

Redis was stopped for approximately 25 seconds after DB recovery.

- `/health` returned degraded with Redis false and database true; `/aircraft` remained HTTP 200 and the persistent WebSocket stayed alive.
- Upstream offered rows stopped at 1,250 while Redis was unavailable. Existing aircraft aged and were evicted; the browser showed STALE aircraft, as expected when token-dependent polling cannot proceed.
- Redis health returned by 05:24:17. Polling resumed after the scheduler backoff: offered rows reached 1,288 / inserted 937 at 05:24:37, then 1,364 / 1,001 at 05:24:42. No token values were inspected.
- By the end of observation, 100 live aircraft, 1,225 inserted rows, 153 drops, and HTTP 200 confirmed recovery. Tick last/p95: 0.813 / 2.019 ms.

This is graceful degradation of live serving, not continuous upstream polling through a Redis outage. Historical DB requests still depend on Postgres; cold startup still requires dependencies.

### Origin and heartbeat

Unit tests cover denied foreign origins, the literal `null` origin, a localhost-lookalike hostname, a configured HTTPS origin, and no Origin. A real handshake from `https://evil.example` received **HTTP 403** (the pre-accept WebSocket close maps to handshake rejection). Allowed-origin probes connected. Uvicorn's configured defaults are 20-second protocol ping interval and timeout.

### Browser behavior and practical performance

The existing local Vite app was observed in the Codex browser. Aircraft glyphs, varied headings, altitude colors, stale fading, HUD, zoom, selected-aircraft details and the recent-track display remained functional. During the Redis outage the selected aircraft became STALE and returned to LIVE after polling recovery. No rendering errors appeared before the deliberate API interruption.

With `?perf=1`, each sample covers feature construction plus the awaited MapLibre source/worker update. Normal operation does not collect samples. Representative 100-update windows:

| Aircraft at window end | Window ms | Update p50 ms | Update p95 ms | Max ms | Busy calls skipped |
| --- | --- | --- | --- | --- | --- |
| 100 | 9,698.8 | 1.5 | 3.5 | 11.3 | 0 |
| 100 | 9,900.6 | 2.3 | 3.6 | 6.0 | 0 |
| 99 | 9,599.4 | 2.1 | 3.7 | 6.0 | 0 |
| 97 | 9,699.9 | 2.3 | 4.0 | 5.8 | 0 |
| 96 | 10,000.4 | 2.4 | 4.1 | 5.2 | 2 |

This supports the 100 ms / approximately 10 fps update budget at the current roughly 100-aircraft load. Occasional busy calls were skipped rather than queued. Counts are the final count in each window, not a claim of constant population. The counter includes any source-update call suppressed by the busy guard. This is not GPU presentation timing or proof of performance at thousands of aircraft.

For reconnect acceptance, the API was stopped for 30 seconds with automatic restoration. The browser showed zero visible/LIVE/STALE aircraft and “Retrying automatically”; the basemap and zoom 6.5 remained intact. Console retry intervals began approximately 0.588 s, 1.873 s and 2.345 s, consistent with exponential equal jitter. The retry cap and timer cleanup are explicit in source. At 05:29:09 UTC it reconnected automatically without a reload; the same zoom 6.5 viewport repopulated to 39 aircraft (37 LIVE / two STALE at observation).

## Staleness evidence

README records the previously measured p50=9 s, p90=17 s and p99=29 s, with `STALE_AFTER_S=20` and `LOST_AFTER_S=60`. These were supplied by the earlier build conversation; this pass did not fabricate raw SQL output or rerun the historical distribution over a different window. The original raw query result/max gap is unavailable. A reproducible query and the deliberate margin above p99 are documented.

## Commands and checks run

Repeated file reads are grouped below; checks with distinct outcomes are retained.

1. Repo review: `pwd`, `rg --files`, `git status --short`, `git log -5 --oneline`, `git diff --stat`, `git diff --check`; targeted `rg`, `cat`, `head` and `sed` reads of API/state/ingestion/config/tests, frontend source, Compose, package metadata, installed MapLibre source signatures, CI and the Step 7 report. Read the synced Phase 3 v2 guide and referenced ChatGPT conversation. Checked parent AGENTS paths; neither existed. README was initially empty.
2. `docker compose ps`: all three services initially running. Initial sandbox access was denied; the authorized retry succeeded.
3. `docker compose exec -T api sh -c 'ruff check . && pytest -v'`: first run stopped on two lint findings in the new test. Fixed only those findings. Rerun: **ruff passed, 104 tests passed** against real TimescaleDB (96 existing + eight new).
4. `npm run build && npm run lint` in `frontend`: passed. Vite reports the existing large bundle warning; oxlint reports two warnings about effect state updates and ref access during cleanup. No dependency/bundle refactor made.
5. `docker compose exec -T api mypy app`, `mypy --no-incremental app`, and `mypy --show-traceback app`: the existing command hit a SQLite cache disk I/O error on the mounted repo. `docker compose exec -T api mypy --cache-dir=/tmp/skywatch-mypy-cache app` completed analysis and reported **seven existing errors**: writer `object`/datetime comparisons (three), OpenSky bytes-or-string return values (three), scheduler optional parse result (one). CI marks this check `continue-on-error`; these untouched ingestion issues remain nonblocking.
6. Lightweight `docker compose exec -T api python` probes: `/health`, `/anomalies?since=&type=`, allowed-origin WebSocket snapshot sizes, 30-second stationary observation, denied-origin handshake, Uvicorn heartbeat defaults.
7. `/tmp/skywatch_failure_drill.py`: orchestrated `docker compose stop db`, `start db`, `stop redis`, `start redis`, plus a persistent API-container Python observer for health, live REST and WebSocket counts. A `finally` restores both dependencies. The script used 25-second outages and waited through recovery; no synthetic writes or volume removals.
8. `docker compose exec -T db psql -U skywatch -d skywatch -c "SELECT region, sum(dropped_count) AS dropped FROM ingest_gaps WHERE reason='db_unavailable' AND started_at >= '2026-09-09 05:22:46+00' GROUP BY region;"`: 73 + 80 = 153 persisted gaps.
9. A Python `try/finally` ran `docker compose stop api`, waited 30 seconds, then `docker compose start api`; browser observations checked clearing, retry spacing and reconnection. One earlier bundled command's automatic approval review timed out before execution; its focused retry succeeded. No action remained blocked by approval review.
10. Browser checks: opened local Vite app, enabled `?perf=1`, read performance/error console logs, inspected screenshots/HUD, clicked an aircraft and closed its detail panel, zoomed, and observed connection loss/recovery. A sandboxed curl to the local frontend returned no usable content; browser access succeeded.

Final verification after the last source edit repeated the backend lint/full suite, frontend build/lint and repository whitespace check. Final results:

| Check | Result |
| --- | --- |
| `ruff check .` | Passed |
| `pytest -q` | 104 passed in 1.85 s; four nonblocking warnings (client deprecations, TestPool collection, mounted pytest cache) |
| `npm run build` | Passed; existing large-chunk warning |
| `npm run lint` | Passed with two warnings |
| `git diff --check` | Passed |
| `docker compose ps` | API running; Postgres and Redis healthy |
| Mypy, container-local cache | Seven pre-existing ingestion typing errors; nonblocking in current CI |

**Phase 3 is complete for local functional acceptance at the measured traffic count**, subject to the explicit limitations below.

## Scope of sign-off

This is local Phase 3 functional acceptance at the current traffic count. It does not claim production deployment, multi-browser validation, a prolonged GPU/frame-pacing profile, or Phase 5 load testing. The remaining optional manual items are a subjective smooth-motion check on the user's usual browser/device and a demo GIF. The original raw historical staleness output cannot be recovered from the available reference. Nonblocking ingestion type errors and build/lint warnings remain explicitly recorded above.
