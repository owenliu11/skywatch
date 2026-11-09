"""Prepare evidence for human review of real recorded detections; never classify automatically."""

import json
import os
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from random import Random

import pyarrow.parquet as pq

from app.detect.features import FeatureComputer
from app.detect.gaps import RecentGaps
from app.detect.rules import RuleDetector
from app.detect.store import AnomalyStore
from app.detect.windows import Sample, TrackWindows

root = Path(os.environ.get("PHASE4_ROOT", str(Path(__file__).resolve().parents[2])))
rows = pq.read_table(root / "eval/recorded_tracks.parquet").to_pylist()
gaps = RecentGaps()
for region, intervals in json.loads((root / "eval/recent_gaps.json").read_text()).items():
    for start, end in intervals:
        gaps.record(region, start, end)
groups = defaultdict(list)
for r in rows:
    groups[(r["icao24"], r["region"])].append(r)
alerts = []
computer = FeatureComputer()
detector = RuleDetector()
for (icao, region), track in sorted(groups.items()):
    windows = TrackWindows()
    store = AnomalyStore()
    for r in track:
        sample = Sample(**{k: r[k] for k in Sample.__dataclass_fields__})
        windows.append_sample(icao, sample)
        if not windows.is_ready(icao):
            continue
        f = computer.compute(icao, windows.get(icao), gaps)
        rule = detector.evaluate(f)
        if rule and f:
            store.hit(rule, f, sample.last_contact)
        store.close_expired(sample.last_contact)
    for alert in (*store.open.values(), *store.pending.values()):
        f = alert.features
        evidence = [r for r in track if f["window_start"] <= r["last_contact"] <= f["window_end"]]
        alerts.append({**asdict(alert), "evidence": evidence})
alerts.sort(key=lambda a: (a["window_start"], a["icao24"], a["detector"]))
for i, alert in enumerate(alerts):
    alert["review_id"] = i + 1
    alert.pop("event_id")
sample = sorted(
    Random(20261001).sample(alerts, min(100, len(alerts))), key=lambda a: a["review_id"]
)
(root / "eval" / os.environ.get("REVIEW_OUTPUT", "real_detection_review.json")).write_text(
    json.dumps(
        {
            "source": "offline replay of unmodified real history; not live detections",
            "n_recorded_rows": len(rows),
            "n_alerts": len(alerts),
            "n_review_sample": len(sample),
            "seed": 20261001,
            "abstentions": dict(detector.abstentions),
            "feature_skips": dict(computer.skipped),
            "alerts": sample,
        },
        indent=2,
    )
    + "\n"
)
print("real replay alerts:", len(alerts), "review sample:", len(sample))
