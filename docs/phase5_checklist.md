# Phase 5 — local cleanup and shipping

Phase 4 finishes with rules, reproducible controlled evaluation and a provisional real-input
error analysis. Keep Track B and public deployment out of scope.

1. Review the working-tree diff before committing: the repository already contained the
   user's A1 changes. Keep those changes and the new Phase 4 files together or separate
   them deliberately; do not reset the pre-existing edits. Check that .env and caches are
   excluded. Inspect the Parquet artifact sizes before deciding whether to keep the frozen
   data in Git or distribute it with a local release bundle. Retain the manifest and hashes.
2. Run the release checks from the README: Ruff, mypy, full Docker pytest, frontend build
   and frontend lint. CI now treats mypy as blocking. Verify a fresh local startup applies
   migration 004 without modifying checksums of migrations 001–003.
3. Build a local demo/replay command that consumes eval/recorded_tracks.parquet through the
   same ingest/broadcast path in a SEPARATE test process/database. Never insert injected
   records into the live history. Record a short demo showing the live map, one real rule
   alert, its details, reload recovery and the before/after evaluation table.
4. Run the Phase 5 synthetic load ramp using generate_track: 100, 500, 1,000 and 5,000
   aircraft. Supply measured report intervals/null rates; measure at each plateau rather
   than extrapolating the smoke result. Record detect_ms_p95, tick_ms_p95, browser render
   p95, loop delay, queue depth/drops, resync count, CPU and memory. Stop increasing load
   when frame/latency budgets fail; publish the measured ceiling and exact machine/setup.
5. Repeat queue-full, DB-unavailable and slow-WebSocket drills with detection enabled.
   Verify live aircraft continue, own-gap abstention increments, pending alerts retry with
   no duplicate INSERTs, and a snapshot restores alert rings. Test orderly shutdown flush
   and explicitly document hard-crash alert loss. Check prolonged-outage pending memory.
6. Collect one uninterrupted local session to recheck the 36 s gap threshold and the
   taxonomy hypotheses. Do not silently tune more rules on the current frozen evaluation;
   use a new aircraft/date-disjoint cohort for any future changes. Address turn-clock and
   low-speed endpoint ambiguity only with new evidence.
7. Clean documentation and UI copy, capture final screenshots, and tag a local release
   (for example v0.4.0 after reviewing the chosen version). Write resume/demo claims only
   from docs/rules_baseline.md and the measured load report. Stop polling after the demo
   with docker compose stop api; keep Docker volumes so recordings survive.

Known optional cleanup: the frontend build warns about its large MapLibre bundle. Split
chunks only if measured startup cost warrants it. No public hosting, authentication rollout,
CARTO dependency, or Isolation Forest is needed to ship this local project.
