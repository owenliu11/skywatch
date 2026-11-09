# Real detection taxonomy — provisional human-readable review

Reviewed **all 54 alerts** from offline replay of **113,650 unmodified recorded observations** from Bay Area/SoCal. These are real-input detections, not injected examples and not a claim of 54 live production alerts. Evidence is frozen in `eval/real_detection_review.json`; the per-alert decisions are in `eval/taxonomy_review.csv`.
Each peak-evidence window was inspected for clocks, reported/implied motion, headings, altitude channels, ground flags and ingestion lag. The classifications below are hypotheses. No receiver logs, independent surveillance, aircraft-type history, or adversarial ground truth were available.

| Guide bucket | Count | Interpretation |
|---|---:|---|
| Our own ingest gap | 0 | No recorded overlap in reviewed alerts; gap windows were abstained upstream. |
| Receiver artifact / dropped message | 4 | Candidate data-quality artifacts, not verified receiver failures. |
| Clock skew | 0 | No convincing clock-skew attribution; ingestion age alone is insufficient. |
| Ground vehicle misclassified | 15 | Low-speed/near-stationary candidates; hovering remains an alternative. |
| Transponder misconfiguration | 0 | Repeated identities observed, but repetition alone does not establish misconfiguration. |
| Genuinely suspicious | 35 | Unresolved residual under the guide taxonomy; this name does NOT establish spoofing. |

`extended=1` is enabled in the OpenSky request. The latest aircraft dimension reports category 0 for 36 of the 37 reviewed identities, and 8 for `aircraft-01`; categories are not time-versioned per observation and are insufficient for firm aircraft-type attribution. All selected alerts have `on_ground=false`, by design.

## One feedback adjustment

Low-speed heading noise is visible directly in the reviewed windows. Gate `impossible_turn` on median reported speed >=25 m/s, reusing the existing ratio floor. No second threshold change was made. This is a partial mitigation: windows with a high median can still contain a near-zero-speed heading discontinuity; legitimate tight turns also remain possible.
Real replay alerts after the change: **42**, down from **54** on the identical recording. This is alert-volume reduction, not a measured false-positive reduction. The 42 remaining events were not relabeled as a second review cohort.
Baseline before/after numbers are in `docs/rules_baseline.md`. No claim such as “85% were artifacts” is supported by this evidence.

## Per-alert review

Aircraft identifiers are replaced with arbitrary labels.

| ID | ICAO24 | Bucket | Evidence / uncertainty |
|---|---|---|---|
| 1 | aircraft-02 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 2 | aircraft-03 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 3 | aircraft-04 | Genuinely suspicious — unresolved residual | Repeated large wrapped turns at 30–55 m/s; coherent motion does not prove a transponder fault. |
| 4 | aircraft-05 | Genuinely suspicious — unresolved residual | Peak involves a zero-speed heading followed by departure; median speed hides the low-speed endpoint. |
| 5 | aircraft-06 | Genuinely suspicious — unresolved residual | Deceleration to zero speed and heading 0; likely unreliable endpoint heading, not confirmed surface classification. |
| 6 | aircraft-07 | Genuinely suspicious — unresolved residual | Rapid turns and changing altitude/velocity; same aircraft also triggers climb (ID 9). Cause unresolved. |
| 7 | aircraft-03 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 8 | aircraft-08 | Genuinely suspicious — unresolved residual | Large heading changes during 8–33 m/s motion; low-speed manoeuvre versus telemetry ambiguity. |
| 9 | aircraft-07 | Genuinely suspicious — unresolved residual | Both altitude channels vary and reported vertical rates are large; insufficient evidence to call this an artifact. |
| 10 | aircraft-05 | Genuinely suspicious — unresolved residual | Zero-speed heading before acceleration; threshold applicability unresolved. |
| 11 | aircraft-09 | Genuinely suspicious — unresolved residual | Persistent rotating headings with roughly 28–40 m/s speed; aircraft type not established. |
| 12 | aircraft-10 | Genuinely suspicious — unresolved residual | Turn during deceleration; ratio near 1; plausible manoeuvre or telemetry artifact. |
| 13 | aircraft-11 | Genuinely suspicious — unresolved residual | Short position-time interval amplifies turn rate during moderate-speed motion. |
| 14 | aircraft-08 | Genuinely suspicious — unresolved residual | Repeated turning on same identity as ID 8; no independent confirmation of equipment fault. |
| 15 | aircraft-02 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 16 | aircraft-03 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 17 | aircraft-04 | Genuinely suspicious — unresolved residual | Repeated turning on same identity as ID 3; no independent confirmation of equipment fault. |
| 18 | aircraft-12 | Genuinely suspicious — unresolved residual | Heading changes across a short interval; modest speed and near-unit speed ratio. |
| 19 | aircraft-02 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 20 | aircraft-03 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 21 | aircraft-13 | Genuinely suspicious — unresolved residual | Rapid heading sequence at 40–58 m/s; physical manoeuvre versus telemetry unresolved. |
| 22 | aircraft-11 | Genuinely suspicious — unresolved residual | Repeated rotating headings; no abnormal ingest delay or confirmed ground flag. |
| 23 | aircraft-02 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 24 | aircraft-14 | Genuinely suspicious — unresolved residual | Heading discontinuity from 4.6 m/s into 96 m/s; low-speed endpoint dominates despite high median. |
| 25 | aircraft-15 | Receiver artifact / dropped message — candidate only | Repeated zero barometric altitude then +609.6 m; geo-altitude also discontinuous while reported climb remains small. |
| 26 | aircraft-03 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 27 | aircraft-16 | Receiver artifact / dropped message — candidate only | Baro/geo altitude jump by about 10.4 km while position and reported vertical rate remain small. |
| 28 | aircraft-02 | Genuinely suspicious — unresolved residual | Turn peak ends near 0.5 m/s; median exceeds floor, so the one guard will not cover every case. |
| 29 | aircraft-05 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 30 | aircraft-17 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 31 | aircraft-18 | Genuinely suspicious — unresolved residual | Rapid rotating headings at 28–32 m/s; ratio <1 but within normal rule range. |
| 32 | aircraft-19 | Genuinely suspicious — unresolved residual | Abrupt wrapped heading sequence with similar implied/reported speeds. |
| 33 | aircraft-20 | Genuinely suspicious — unresolved residual | Abrupt wrapped heading sequence with similar implied/reported speeds. |
| 34 | aircraft-21 | Genuinely suspicious — unresolved residual | Deceleration and repeated turns at 20–60 m/s; no verified cause. |
| 35 | aircraft-22 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 36 | aircraft-23 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 37 | aircraft-24 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 38 | aircraft-11 | Genuinely suspicious — unresolved residual | Repeated turning on same aircraft as IDs 13/22; persistence is not proof of misconfiguration. |
| 39 | aircraft-25 | Ground vehicle misclassified — candidate only | Very low/near-stationary motion with unstable headings and on_ground=false. Could also be hovering aircraft; surface status is unconfirmed. |
| 40 | aircraft-26 | Receiver artifact / dropped message — candidate only | Position age reaches 120 s with repeated coordinates; fresh/stale position transitions contaminate heading evidence. |
| 41 | aircraft-27 | Genuinely suspicious — unresolved residual | Large heading changes with stable moderate speed; no independent cause evidence. |
| 42 | aircraft-28 | Receiver artifact / dropped message — candidate only | 159 s stale position followed by refreshed reports; heading changes over a short valid position interval. |
| 43 | aircraft-29 | Genuinely suspicious — unresolved residual | Sharp turns during deceleration; no evidence of malicious behaviour. |
| 44 | aircraft-30 | Genuinely suspicious — unresolved residual | Heading jump during deceleration; near-unit speed ratio. |
| 45 | aircraft-31 | Genuinely suspicious — unresolved residual | Rapid heading changes at 20–33 m/s; floor does not fully address threshold applicability. |
| 46 | aircraft-32 | Genuinely suspicious — unresolved residual | Short interval with a large turn while slowing; plausible manoeuvre versus telemetry unresolved. |
| 47 | aircraft-33 | Genuinely suspicious — unresolved residual | Heading peak occurs near walking speed but median is 30 m/s; remains a known limitation. |
| 48 | aircraft-28 | Genuinely suspicious — unresolved residual | 18.4 deg change over a one-second position interval; the 1 s floor allows it. |
| 49 | aircraft-34 | Genuinely suspicious — unresolved residual | Repeated large heading changes; no matching own-gap evidence. |
| 50 | aircraft-26 | Genuinely suspicious — unresolved residual | Repeated rapid turns on identity from ID 40, but this window has fresh positions. |
| 51 | aircraft-35 | Genuinely suspicious — unresolved residual | Repeated turning during deceleration; near-unit speed ratio. |
| 52 | aircraft-01 | Genuinely suspicious — unresolved residual | Category value 8 in latest aircraft metadata; no time-versioned category evidence. Rapid turns at modest speed. |
| 53 | aircraft-36 | Genuinely suspicious — unresolved residual | Short intervals plus stale-position ages up to 6 s; no confirmed upstream failure. |
| 54 | aircraft-37 | Genuinely suspicious — unresolved residual | Rapid heading changes at 39–50 m/s; underlying cause remains unresolved. |
