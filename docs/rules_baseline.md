# Rules baseline — Phase 4, local only

## Frozen dataset

Seed **20261001**. Query cutoff **2026-10-01T00:21:39.449444+00:00**. Active regions: **Bay Area and SoCal**, per user correction; Denver is excluded.
615 contiguous recorded sessions (minimum 30 samples), 30 injected sessions (4.88%; floor of 5%). Seven variants, 4–5 events each. No model was trained.
Aircraft are partitioned by SHA256(icao24) modulo 2. Even identities before 2026-09-30 UTC are reserved; odd identities on/after that date are evaluated. Reserved IDs and every evaluated session are in `eval/manifest.json`. No training/serving of Isolation Forest.
Only injection candidates require non-null motion fields, advancing valid position times, and airborne samples. Unmodified controls retain missing data and ground observations. This biases injected positives toward good telemetry; exclusions must be read alongside recall.
Dataset SHA256: `ba90c53218fb75ac475bb314de5225c71d276da76048b3eea89b94b21a953ca1`.

## Predeclared evaluation semantics

- A candidate is an accepted 5–10-sample window, replayed after each new observation. Warm-up samples and rejected inputs are not candidate windows.
- A positive window overlaps the inclusive injected index range. Ground, ingest-gap, and uncomputable windows are excluded and counted. Event recall retains every injected event in its denominator, including events with excluded windows.
- Window precision/recall/F1 use any rule hit versus the injection label. They do not claim attribution to a particular attack type.
- Alert precision uses the actual 120-second `(aircraft, detector)` merge. An alert is positive if at least one constituent hit overlaps the label. Different rules can still create separate alerts for one event.
- Sessions are independent evaluation identities; cooldown does not merge across session boundaries. Live scoring samples at broadcast cadence; offline scoring sees every accepted stored observation. Stored history may contain write-path omissions.
- Each per-type row includes all clean control sessions plus that type’s injected sessions. Those denominators overlap and must not be summed.
- Unmodified controls are assumed negative, not manually proven benign. Controlled synthetic anomalies are an optimistic proxy, not validated performance against real adversarial behaviour.
- Undefined ratios are null in JSON and shown as —. No zero substitution for undefined precision.

## Before and after the single Track C adjustment

Only `impossible_turn` changed: require median reported speed >=25 m/s. The original 15 deg/s threshold and every other rule remain unchanged. The same frozen artifact was rescored. Real review used overlapping source history, so the post-review score is a diagnostic comparison, not a new independent holdout estimate.

| Rules | Window P | Window R | F1 | Events caught | Alert P | Candidate windows | Excluded |
|---|---:|---:|---:|---:|---:|---:|---:|
| Original | 57.64% | 62.94% | 60.18% | 22/30 | 53.33% | 50515 | 6958 |
| After low-speed turn guard | 69.12% | 62.94% | 65.89% | 22/30 | 58.18% | 50515 | 6958 |

## Per-type results after adjustment

| Type | Window P | Window R | F1 | Events caught | Alert P | Candidate windows | Excluded |
|---|---:|---:|---:|---:|---:|---:|---:|
| duplicate_identity | 43.09% | 95.29% | 59.34% | 4/4 | 32.35% | 48519 | 6958 |
| frozen_position | 26.46% | 75.49% | 39.19% | 4/4 | 23.33% | 48315 | 6928 |
| impossible_climb_a | 33.12% | 96.36% | 49.30% | 4/4 | 14.81% | 48374 | 6928 |
| impossible_climb_b | 26.71% | 95.12% | 41.71% | 4/4 | 14.81% | 48227 | 6928 |
| slow_drift | 2.73% | 4.55% | 3.41% | 1/4 | 4.17% | 48492 | 6928 |
| teleport | 18.94% | 100.00% | 31.85% | 5/5 | 17.86% | 48485 | 6928 |
| velocity_inconsistency | 0.00% | 0.00% | 0.00% | 0/5 | 0.00% | 48361 | 6928 |

Exclusions: {'on_ground': 6784, 'uncomputable': 174}; excluded fraction 13.77%. Positive excluded windows: 30. No matching recorded ingest-gap windows in this evaluation.

## What failed and why

- Velocity inconsistency uses reported speed ×0.1. All five events were missed: the corrupted reported speed often drops below the deliberate 25 m/s ratio floor. This is a measured blind spot, not evidence that every velocity inconsistency is undetectable. A separate controlled test covers an above-floor mismatch.
- Slow drift is 10 m/s lateral fabricated motion. One of four events overlapped a rule hit; noisy real base tracks can trigger unrelated rules, so this is not proof that the rules detect subtle drift.
- Teleport magnitude is 50 km; climb variants add 60 m/s altitude ramps; frozen positions retain advancing position timestamps. Duplicate identity uses a timestamp-interleaved, spatially shifted constructed donor, not a second independently observed aircraft. These magnitudes and the donor construction limit generalization.
- Both climb variants were caught in all four events: the absolute implied-climb rule does not require a vertical mismatch. The guide’s expected poor variant-B result does not follow from that rule.
- Alert precision remains below window precision in this experiment. Merging does not guarantee a precision improvement.

## Reproduce without querying live data

```sh
cd backend
../.venv/bin/python -m pip install -e ".[dev,eval]"
../.venv/bin/python -m app.detect.evaluate ../eval/labeled_set.parquet ../eval/rules_metrics.json
PYTHONPATH=. REVIEW_OUTPUT=real_detection_review_after.json ../.venv/bin/python scripts/review_real.py
```

To build a NEW dataset from local history: `PYTHONPATH=. ../.venv/bin/python scripts/build_evaluation.py` (default DB localhost:55432, or DATABASE_URL). This changes the query cutoff and overwrites the current artifacts; preserve a copy first. `eval/recent_gaps.json` is automatically loaded during frozen evaluation. `pyarrow` is an optional evaluation dependency, not required for live detection.
