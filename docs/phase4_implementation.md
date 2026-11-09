# Phase 4 implementation decisions

Reduced scope: A2–A9 plus C; no Track B, no deployment, no CARTO. Bay Area and SoCal
remain the active regions.

## Window and feature contract

- GAP_S=36 from the maximum active-region p99 in docs/gap_distribution.txt.
- Strict last_contact ordering; reject missing positions without advancing the window;
  capacity 10, readiness 5, maximum span 180 s. Detection eviction uses the exact same
  broadcast tick cutoff as LiveState (60 seconds). No database backfill after restart.
- Valid motion pairs require both position clocks, position dt in [1,36] s, finite
  coordinates, reported velocity and both headings. Invalid pairs are counted and skipped.
  Contact intervals must also be in [1,36] s. A window without any usable pair returns None.
- Median implied speed, median reported speed, median per-pair speed ratio. A ratio is
  computable only where reported speed >=25 m/s. Median contact interval and population
  interval variance. Jump distance and speed are maxima across valid pairs.
- Signed peak absolute turn rate, turn acceleration, reported vertical rate and implied
  vertical rate. Turn acceleration requires adjacent usable pairs and divides the rate
  difference by the mean of their durations. Vertical mismatch is peak absolute error.
- Motion, heading and altitude derivatives use position time; contact intervals use
  last_contact. Altitude/report timestamps are not independently provided by OpenSky;
  the chosen altitude/heading convention is a limitation when fields arrive asynchronously.
- Vertical values and acceleration can remain None without suppressing unrelated rules.
  No nullable feature is filled with zero; metadata is separate from the 12 scored fields.
  n_pairs means usable motion pairs, not necessarily valid pairs for every optional feature.
- Angle delta is [-180,180); the guide's formula has this range despite its prose saying
  (-180,180]. Haversine clamps roundoff at antipodes.
- Median motion versus peak jump resolves the guide's ambiguous aggregation: sustained
  impossible speed has first priority; a brief jump can reach teleport independently.
- On-ground and region-matched ingest-gap windows abstain. Gaps use the existing writer's
  recorder, retained in memory for 600 seconds, never queried per tick.
- Exactly one feedback change: impossible_turn also requires median reported speed >=25 m/s.
  The same threshold does not remove every low-speed endpoint in a faster window.

## Live lifecycle and API

Ingest -> LiveState + TrackWindows -> existing bounded history queue. One synchronous
scoring pass runs in the existing broadcast task. A last-scored timestamp prevents the
same unchanged window from incrementing hit_count every second. Persistence is a separate
async task; database latency cannot hold up scoring or the aircraft frame producer.

Open alerts merge by (icao24, detector); 120 seconds without a newer hit closes them.
Closed alerts remain in a pending retry set until an idempotent INSERT succeeds. event_id
has a unique constraint: an ambiguous commit cannot duplicate an event. No anomaly UPDATE
or synthetic writes occur. Shutdown closes and attempts to flush remaining alerts.

Crash limitation: open alerts and pending retry state are process memory. They can be lost
on a hard crash. Contrary to the guide's shorthand, a continuously extended alert can be
older than 120 seconds, so loss is NOT necessarily limited to the last two minutes. A long
DB outage can grow pending memory; this is a local demo, not a durable system of record.

GET /anomalies returns {since,until,window_clamped,truncated,count,anomalies}, with event
and feature timestamps as epoch seconds. Bounds accept timezone-aware ISO datetimes.
Default range 1 hour, maximum 24 hours relative to until; default limit 500, maximum 5000.
It filters by window_start in [since,until), optional type (with/without rule:), case-normalized
ICAO24, and include_synthetic=false. Rows are ordered by window_start descending. A memory
snapshot taken before the DB await is unioned/deduplicated with persisted rows, so a
concurrent flush cannot hide an event. Both query bounds are always present in SQL.
Long-running events whose original window_start is older than the requested range are not
included; expand the query up to 24 hours if needed. DB errors still make the endpoint
unavailable even though live scoring continues; the UI reports alert-history unavailability.

Anomaly opens emit a separate t=anomaly frame through the existing per-client queue and
sequence counter. Aircraft FIELDS stays unchanged. Dropped frames force a snapshot, and
all snapshots trigger alert-history refetch. Initial connect, reconnect, viewport snapshots
and a 60-second periodic refresh recover alerts. The map ring lasts an hour after the latest
known event window_end. The frontend fetch limit is 5000 and surfaces truncation explicitly.

Health includes ready-window count, computable/skipped feature and pair counts, rule hits,
abstentions, open/pending/flushed alerts, persistence errors, detector tick last/p95, and
model_version=null. The existing aircraft tick time includes synchronous detection cost.
Timings include idle ticks and are local smoke observations, not load-test capacity.

## Offline data and honesty

See rules_baseline.md for the split, seed, artifact hash, aggregation units, exclusions,
per-type results and blind spots. Parquet dependencies are evaluation-only. The labeled
artifact preserves original icao24 as metadata while each contiguous session is an
independent replay identity. Synthetic clean generation supports caller-supplied measured
interval distributions and null rates; it is not fitted or used to train a detector.

The guide overstates what automatic taxonomy buckets can establish. taxonomy.md reports
all 54 manually inspected evidence windows with provisional attribution and an unresolved
bucket. Neither repeated aircraft-specific alerts nor low speed alone proves a equipment
fault or a misclassified ground vehicle. No spoofing success claim is made.
