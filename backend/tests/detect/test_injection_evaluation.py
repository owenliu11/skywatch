from dataclasses import asdict, replace
from itertools import pairwise

import pytest

from app.detect.evaluate import evaluate_rows
from app.detect.features import FeatureComputer
from app.detect.inject import AnomalyInjector
from app.detect.synthetic import generate_track


def clean():
    return generate_track(duration_s=400, seed=42, interval_dist=lambda: 10, null_rates={})[0]


def test_generator_is_seeded_and_physically_coherent():
    track = clean()
    assert track == clean()
    f = FeatureComputer().compute("a", tuple(track[:10]))
    assert f and f.speed_mismatch_ratio == pytest.approx(1, abs=1e-8)
    assert f.vertical_mismatch_ms == pytest.approx(0, abs=1e-8)
    missing = generate_track(
        duration_s=50, seed=42, interval_dist=lambda: 10, null_rates={"velocity": 1}
    )[0]
    assert all(s.velocity is None for s in missing)


@pytest.mark.parametrize(
    "kind,magnitude",
    [
        ("teleport", 50000),
        ("velocity_inconsistency", 0.1),
        ("impossible_climb_a", 60),
        ("impossible_climb_b", 60),
        ("frozen_position", 0),
        ("slow_drift", 10),
    ],
)
def test_injections_preserve_clocks_and_source(kind, magnitude):
    original = clean()
    track, label = AnomalyInjector().inject(original, kind, 12, 25, magnitude)
    assert original == clean()
    assert [(s.last_contact, s.time_position) for s in track] == [
        (s.last_contact, s.time_position) for s in original
    ]
    if kind == "frozen_position":
        assert all((s.lat, s.lon) == (original[11].lat, original[11].lon) for s in track[12:26])
        assert all(b.time_position > a.time_position for a, b in pairwise(track))
    if kind == "teleport":
        assert track[13:] == original[13:]
        assert label.end == 12
    if kind == "impossible_climb_b":
        f = FeatureComputer().compute("a", tuple(track[15:25]))
        assert f and f.vertical_mismatch_ms == pytest.approx(0, abs=1e-8)
        assert f.implied_vertical_rate_ms > 40.6


def test_duplicate_identity_sort_and_clock_age():
    original = clean()
    donor = [
        replace(
            s, last_contact=s.last_contact + 5, time_position=s.time_position + 5, lat=s.lat + 1
        )
        for s in original
    ]
    track, label = AnomalyInjector().inject(original, "duplicate_identity", 12, 25, 1000, donor)
    assert len(track) > len(original)
    assert all(b.last_contact > a.last_contact for a, b in pairwise(track))
    assert all(s.last_contact - s.time_position == 0 for s in track)
    assert label.start >= 12


def test_evaluation_event_merge_and_drift_miss():
    rows = []
    for kind, magnitude in [("teleport", 50000), ("slow_drift", 10)]:
        track, label = AnomalyInjector().inject(clean(), kind, 12, 25, magnitude)
        for i, s in enumerate(track):
            rows.append(
                {
                    "track_id": kind,
                    "sample_index": i,
                    "injection_type": kind,
                    "label_start": label.start,
                    "label_end": label.end,
                    **asdict(s),
                }
            )
    metrics = evaluate_rows(rows)
    assert metrics["events"] == 2
    assert metrics["events_caught"] == 1
    assert metrics["alert_precision"] == 1
    assert metrics["counts"]["tp"] > 1
    assert metrics["alerts"] < metrics["counts"]["tp"]
    assert metrics["counts"]["fp"] == 0
    assert metrics["counts"]["fn"] > 0


def test_exclusions_are_counted_and_undefined_metrics_are_null():
    rows = [
        {
            "track_id": "ground",
            "sample_index": i,
            "injection_type": "clean",
            "label_start": -1,
            "label_end": -1,
            **asdict(replace(s, on_ground=True)),
        }
        for i, s in enumerate(clean())
    ]
    result = evaluate_rows(rows)
    assert result["excluded_fraction"] == 1
    assert result["window_precision"] is None
    assert result["alert_precision"] is None


def test_velocity_mismatch_above_floor_is_detectable():
    from app.detect.rules import RuleDetector

    track, _ = AnomalyInjector().inject(clean(), "velocity_inconsistency", 12, 25, 4)
    f = FeatureComputer().compute("a", tuple(track[15:25]))
    assert f is not None and f.reported_speed_ms >= 25
    assert RuleDetector().evaluate(f) == "rule:speed_mismatch"


def test_frozen_stale_position_is_not_mislabeled():
    track = clean()
    track[15] = replace(track[15], time_position=track[14].time_position)
    with pytest.raises(ValueError, match="advancing"):
        AnomalyInjector().inject(track, "frozen_position", 12, 25, 0)
