"""Repeatable offline evaluation; no synthetic data enters the production database."""

import json
from collections import Counter, defaultdict
from pathlib import Path

from app.detect.features import FeatureComputer
from app.detect.gaps import RecentGaps
from app.detect.rules import RuleDetector
from app.detect.store import AnomalyStore
from app.detect.windows import MIN_SAMPLES, Sample, TrackWindows


def fraction(a: int, b: int) -> float | None:
    return a / b if b else None


def evaluate_rows(rows: list[dict], gaps: RecentGaps | None = None) -> dict:
    tracks: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        tracks[row["track_id"]].append(row)
    counts: Counter[str] = Counter(
        {
            key: 0
            for key in (
                "tp",
                "fp",
                "fn",
                "tn",
                "candidate_windows",
                "positive_excluded",
                "negative_excluded",
            )
        }
    )
    exclusions: Counter[str] = Counter()
    events = 0
    caught = 0
    alerts = 0
    correct_alerts = 0
    details = []
    for track_id, track in sorted(tracks.items()):
        track.sort(key=lambda r: r["sample_index"])
        windows = TrackWindows()
        computer = FeatureComputer()
        detector = RuleDetector()
        store = AnomalyStore()
        positive_hits: set[str] = set()
        kind = track[0]["injection_type"]
        has_event = kind != "clean"
        events += int(has_event)
        event_hit = False
        for row in track:
            sample = Sample(**{k: row[k] for k in Sample.__dataclass_fields__})
            if not windows.append_sample(track_id, sample):
                continue
            window = windows.get(track_id)
            if len(window) < MIN_SAMPLES:
                continue
            positive = has_event and (
                row["sample_index"] >= row["label_start"]
                and row["sample_index"] - len(window) + 1 <= row["label_end"]
            )
            counts["candidate_windows"] += 1
            f = computer.compute(track_id, window, gaps)
            reason = (
                "uncomputable"
                if f is None
                else "on_ground"
                if f.on_ground_any
                else "ingest_gap"
                if f.spans_gap
                else None
            )
            if reason:
                exclusions[reason] += 1
                counts["positive_excluded" if positive else "negative_excluded"] += 1
                continue
            prediction = detector.evaluate(f)
            counts[
                "tp"
                if prediction and positive
                else "fp"
                if prediction
                else "fn"
                if positive
                else "tn"
            ] += 1
            if prediction and f:
                store.hit(prediction, f, sample.last_contact)
                event = store.open[(track_id, prediction)]
                if positive:
                    positive_hits.add(event.event_id)
                    event_hit = True
            store.close_expired(sample.last_contact)
        caught += int(event_hit)
        all_alerts = (*store.open.values(), *store.pending.values())
        alerts += len(all_alerts)
        correct_alerts += sum(a.event_id in positive_hits for a in all_alerts)
        if has_event:
            details.append({"track_id": track_id, "kind": kind, "caught": event_hit})
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    return {
        "counts": dict(counts),
        "exclusions": dict(exclusions),
        "window_precision": fraction(tp, tp + fp),
        "window_recall": fraction(tp, tp + fn),
        "window_f1": fraction(2 * tp, 2 * tp + fp + fn),
        "events": events,
        "events_caught": caught,
        "event_recall": fraction(caught, events),
        "alerts": alerts,
        "correct_alerts": correct_alerts,
        "alert_precision": fraction(correct_alerts, alerts),
        "excluded_fraction": fraction(sum(exclusions.values()), counts["candidate_windows"]),
        "event_details": details,
    }


def run(dataset: Path, output: Path, gaps: RecentGaps | None = None) -> dict:
    import pyarrow.parquet as pq

    if gaps is None and (dataset.parent / "recent_gaps.json").exists():
        gaps = RecentGaps()
        for region, intervals in json.loads(
            (dataset.parent / "recent_gaps.json").read_text()
        ).items():
            for start, end in intervals:
                gaps.record(region, start, end)
    rows = pq.read_table(dataset).to_pylist()
    result = {"overall": evaluate_rows(rows, gaps), "per_type": {}}
    kinds = sorted({r["injection_type"] for r in rows} - {"clean"})
    for kind in kinds:
        result["per_type"][kind] = evaluate_rows(
            [r for r in rows if r["injection_type"] in ("clean", kind)], gaps
        )
    output.write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.dataset, args.output)
