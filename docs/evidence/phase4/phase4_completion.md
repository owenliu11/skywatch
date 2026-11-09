# Phase 4 completion report

Active regions remain Bay Area and SoCal. Local-only; no Track B.

## Delivered

- A2: per-ICAO24 strict sliding windows, 5–10 samples, gap/span resets, position rejection,
  readiness and shared live-map eviction. Measured GAP_S=36; calibration evidence saved.
- A3: all 12 features, Haversine, angle wrapping, two clocks, dt/ratio guards, no imputation,
  separate metadata and window/pair skip counters. Aggregation contracts documented.
- A4: five mutually exclusive prioritized rules, ground and region-aware gap abstention.
  One evidence-based change gates impossible_turn at the existing 25 m/s speed floor.
- A5: one scoring pass per broadcast tick, changed-window deduplication, 120 s open merge,
  insert-only idempotent persistence with pending retries, migration, bounded filtered
  /anomalies union, separate WebSocket frame, snapshot recovery, ring/detail UI and health.
- A6/A7: seeded coherent generator and seven injection variants (including both climbs),
  original timestamps preserved, frozen-position freshness validation, identity/date split,
  615 real-source evaluation sessions, 30 injected events, labeled Parquet and manifest.
- A8/A9: window P/R/F1, event recall, merged-alert precision, explicit exclusions,
  per-variant results, exact reproducibility and before/after baseline artifacts.
- C: all 54 real-recording replay alerts inspected; per-alert provisional taxonomy.
  Same recording yields 42 alerts after the one guard. No adversarial-attribution claims.

## Results and limitations

Final window precision 69.12%, recall 62.94%, F1 65.89%; event recall 22/30 (73.33%);
merged-alert precision 58.18%; 13.77% of candidate windows excluded. The smaller per-type
samples have only 4–5 events. Velocity ×0.1 is missed below the ratio floor; subtle drift
remains weak. Controls are assumed negative. Post-review metrics are not an independent
new holdout. Recording is intermittent; it does not establish continuous coverage.

At the captured smoke instant: 389 windows, 360 ready, detector p95 10.70 ms, total tick
p95 12.38 ms. Timing includes idle ticks and is not a load-ramp result. Open/pending alerts
can be lost on a hard crash; long-lived alerts are not bounded to two minutes of loss.
Pending memory is not capped during extended DB outages. Original pre-adjustment real
alerts remain in the insert-only database; the adjustment does not rewrite history.

See docs/rules_baseline.md, docs/taxonomy.md and docs/phase4_implementation.md for the full contracts.

## Verification

- docker compose exec -T api pytest -v: **131 passed**, three existing warnings.
- mypy app: **clean, 28 source files** (cache stored in /tmp for sandbox compatibility).
- ruff check .: **clean**; git diff --check clean.
- npm run build and npm run lint: **pass**. Existing large-bundle warning remains.
- Dataset SHA256 matches manifest; 529 evaluated aircraft identities are disjoint from
  reserved earlier identities; frozen evaluation reproduces the metrics JSON exactly.
- Browser smoke: real live map, pink ring on a flagged aircraft, impossible_turn detail label,
  reload/snapshot history recovery. Backend tests cover actual outbound queue overflow,
  next-snapshot behavior, endpoint union and transaction-rollback persistence/retry.
  A deliberately dropped frame was not injected into the live browser connection.
- No injected evaluation samples were written to the live database. Database integration
  test rows are rolled back. A1's original typing fixes and unrelated Phase 3 behavior
  were preserved. Mypy is now blocking in CI.

## File inventory

New/replaced detection modules under backend/app/detect/:
windows.py, features.py, gaps.py, rules.py, store.py, pipeline.py, synthetic.py,
inject.py, evaluate.py (existing package __init__.py retained).

Integration changes:
backend/app/main.py; backend/app/api/ws.py; backend/app/api/anomalies.py;
backend/app/ingest/writer.py; backend/migrations/004_anomalies.sql;
backend/pyproject.toml (optional eval dependency); frontend/src/App.tsx;
frontend/vite.config.ts; .github/workflows/ci.yml; README.md.

New tests under backend/tests/detect/:
test_windows.py, test_features_rules.py, test_pipeline.py,
test_injection_evaluation.py. Updated existing test_ingest_bridge.py and
removed obsolete /anomalies stub assertions from test_phase3_completion.py.
Existing user changes in config.py, opensky.py, scheduler.py, test_lifespan.py and
other A1 files remain in the working tree; they were not reverted or committed.

New scripts:
backend/scripts/build_evaluation.py; backend/scripts/review_real.py.

Evaluation artifacts:
eval/labeled_set.parquet; eval/recorded_tracks.parquet; eval/manifest.json;
eval/recent_gaps.json; eval/sampling_profile.json; eval/rules_metrics_before.json;
eval/rules_metrics.json; eval/real_detection_review.json;
eval/real_detection_review_after.json; eval/taxonomy_review.csv.

Reports:
docs/gap_distribution.txt; docs/rules_baseline.md; docs/taxonomy.md;
docs/phase4_implementation.md; docs/evidence/phase4/phase4_completion.md; docs/phase5_checklist.md;
docs/evidence/phase4/phase4_health.json; docs/evidence/phase4/phase4_reproducibility.txt; docs/evidence/phase4/phase4_pytest.txt;
docs/evidence/phase4/phase4_mypy.txt; docs/evidence/phase4/phase4_ruff.txt; docs/evidence/phase4/phase4_frontend_build.txt;
docs/evidence/phase4/phase4_frontend_lint.txt. Shutdown verification is recorded separately.

The two Parquet files total approximately 3.3 MB. No commit, deployment, model artifact,
PR curve, drift-sweep chart, or public service was created.

## Next

Follow phase5_checklist.md in order: review/commit the combined A1+Phase4 diff, run clean
local startup checks, make a separate replay demo, measure the synthetic load ramp, repeat
failure drills with detection enabled, validate on a new recording cohort, then tag a local
release and prepare measured resume/demo claims. No Isolation Forest is needed.
