# Testing and TestForge integration

SkyWatch is the first target of TestForge, an external system-test harness. This page is the contract between them. TestForge should pin a specific SkyWatch commit; `v0.4.0` is the first one with these controls.

## System under test

| Area | Behavior |
|---|---|
| Processes | `api`, `db` (TimescaleDB), `redis`, and a separately run Vite frontend |
| In-process state | Polling, `LiveState`, detector windows, credit budget and metrics live in the API process |
| Redis | OAuth token cache and refresh lock only; it holds no aircraft data |
| Development ports | API 8000, frontend 5173, database 55432; Redis is not published |
| External traffic | OpenSky at runtime, and OpenStreetMap tiles from the browser |

Seeding the database does not populate the live map. Restart the API between independent scenarios, because resetting storage does not clear in-memory state.

## Test settings

| Setting | Purpose |
|---|---|
| `OPENSKY_TOKEN_URL`, `OPENSKY_STATES_URL` | Point the client at a provider fixture |
| `SKYWATCH_TEST_MODE=true` | Disable continuous polling and enable `/__test` routes |
| `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET` | Must be non-empty dummy values, or the token request fails first |
| `POLL_REGIONS=bay_area` | Limit each cycle to one provider request |
| `WS_ALLOWED_ORIGINS` | Must include the UI origin the browser uses |
| `VITE_WS_URL` | Build-time WebSocket URL; defaults to `ws://localhost:8000/ws/live` |
| `API_PROXY_TARGET` | Vite proxy target; defaults to `http://localhost:8000` |

Test mode refuses to start if either provider URL points at `opensky-network.org`. Run one Uvicorn worker without `--reload`. The `/__test` routes do not exist unless test mode is on.

The provider fixture must serve the OAuth token and `/states/all` responses. State rows are positional arrays of 17 or 18 fields.

Use synthetic fixtures only. Never commit recorded OpenSky data or real aircraft identifiers.

## Ingestion control

```http
POST /__test/ingest/cycles
{"scenario_id": "normal"}
```

The request returns after one fetch and ingest pass for every enabled region. A failed cycle still returns HTTP 200, with `status: "failed"`.

```json
{
  "cycle_id": "9f3c...",
  "scenario_id": "normal",
  "status": "succeeded",
  "started_at": "2026-11-09T22:00:00+00:00",
  "finished_at": "2026-11-09T22:00:00.040000+00:00",
  "accepted": 1,
  "rejected": 0,
  "regions": [
    {"region": "bay_area", "ok": true, "status_code": 200, "accepted": 1, "rejected": 0, "error": null}
  ]
}
```

`GET /__test/ingest/cycles/{cycle_id}` returns the same record for the latest 256 cycles.

A completed cycle means ingest handling finished. `accepted` counts rows that parsed, not rows stored:

- Live data: poll `/aircraft`.
- History: poll `/aircraft/{icao24}/track`, because writes are asynchronous and can be dropped.

## Time

There is no injectable clock. Aircraft older than 60 seconds are evicted on the next one-second tick, so generate fixture timestamps relative to the current time. Sending `now - 58` lets a test observe eviction within a few seconds. The detector needs 5 strictly increasing samples per aircraft and resets across gaps over 36 seconds.

## Browser

- Stable locators: connection `role="status"`, the `Aircraft in the current viewport` counts, operational messages, and the `Close aircraft details` button.
- Aircraft are drawn on a WebGL canvas and cannot be selected through the DOM. Use a fixed viewport and known fixture position, then assert on the details panel.
- Stub requests to `tile.openstreetmap.org` for offline runs.

## Known defects

These are deliberately open so TestForge can capture them failing before they are fixed.

| Defect | Trigger | Effect |
|---|---|---|
| Polling loop dies | Snapshot `time` outside Python's datetime range, such as `10**14` | `handle_ingest_result` raises outside the scheduler's guard, and that region's polling stops |
| Broadcast loop dies | Aircraft `last_contact` outside the datetime range | `LiveState.evict_before` raises, and all WebSocket updates stop |
| Dead tasks are invisible | Either failure above | `/health` checks only the database and Redis, so it still reports `ok` |

The polling defect needs continuous polling: use the fixture URLs without test mode, because a test-mode cycle records the error as a failed cycle. The broadcast defect reproduces in test mode.

## Not provided

- A frontend Dockerfile or Compose service; TestForge owns its own test Compose file.
- A controlled application clock or offline map style.
- A license file. Choose one before reusing or redistributing this code.
