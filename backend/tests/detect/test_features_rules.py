from dataclasses import replace

import pytest

from app.detect.features import FEATURE_COLUMNS, FeatureComputer, ang_delta, haversine
from app.detect.gaps import RecentGaps
from app.detect.rules import RuleDetector
from app.detect.windows import Sample


def track():
    return tuple(
        Sample(i * 10, i * 10, 0, i * 0.02, 10000, 10050, 222.38985, 90, 0, False, "bay_area")
        for i in range(10)
    )


def test_geometry_and_metadata():
    assert ang_delta(1, 359) == 2
    assert ang_delta(359, 1) == -2
    assert haversine(0, 0, 0, 1) == pytest.approx(111194.927, rel=1e-6)
    f = FeatureComputer().compute("a", track())
    assert f and f.implied_speed_ms == pytest.approx(222.38985, rel=1e-6)
    assert set(f.scored()) == set(FEATURE_COLUMNS)
    assert RuleDetector().evaluate(f) is None


def test_two_clocks_and_bad_motion_dt():
    t = tuple(replace(s, time_position=i * 20 - 80) for i, s in enumerate(track()))
    f = FeatureComputer().compute("a", t)
    assert f and f.update_interval_s == 10
    assert f.implied_speed_ms == pytest.approx(111.1949, rel=1e-5)
    for dt in (0, 0.5, -1, 37):
        computer = FeatureComputer()
        assert (
            computer.compute(
                "a", tuple(replace(s, time_position=i * dt) for i, s in enumerate(track()))
            )
            is None
        )
        assert computer.pairs_skipped["dt_invalid"] == 9


def test_missing_and_floor():
    for field, reason in [("time_position", "no_time_position"), ("velocity", "null_velocity")]:
        c = FeatureComputer()
        assert c.compute("a", tuple(replace(s, **{field: None}) for s in track())) is None
        assert c.skipped[reason] == 1
    f = FeatureComputer().compute("a", tuple(replace(s, velocity=0.5) for s in track()))
    assert f and f.speed_mismatch_ratio is None
    assert RuleDetector().evaluate(f) is None
    f = FeatureComputer().compute(
        "a", tuple(replace(s, baro_alt=None, vertical_rate=None) for s in track())
    )
    assert f and f.implied_vertical_rate_ms is None and f.vertical_mismatch_ms is None


def test_angles_and_priority():
    t = tuple(replace(s, true_track=(359 + i * 2) % 360) for i, s in enumerate(track()))
    f = FeatureComputer().compute("a", t)
    assert f and f.turn_rate_deg_s == pytest.approx(0.2)
    detector = RuleDetector()
    for change, name in [
        ({"implied_speed_ms": 700, "jump_speed_ms": 1000}, "impossible_speed"),
        ({"jump_speed_ms": 1000}, "teleport"),
        ({"implied_vertical_rate_ms": -50}, "impossible_climb"),
        ({"turn_rate_deg_s": 16}, "impossible_turn"),
        ({"speed_mismatch_ratio": 0.1}, "speed_mismatch"),
    ]:
        assert detector.evaluate(replace(f, **change)) == "rule:" + name
    assert sum(detector.detections.values()) == 5


def test_ground_and_region_gap_abstention():
    gaps = RecentGaps()
    gaps.record("socal", 30, 40)
    c = FeatureComputer()
    f = c.compute("a", track(), gaps)
    assert f and not f.spans_gap
    gaps.record("bay_area", 30, 40)
    f = c.compute("a", track(), gaps)
    assert f and f.spans_gap
    d = RuleDetector()
    assert d.evaluate(replace(f, jump_speed_ms=1000)) is None
    assert d.abstentions["ingest_gap"] == 1
    assert d.evaluate(replace(f, spans_gap=False, on_ground_any=True)) is None
    assert d.abstentions["on_ground"] == 1
    gaps.evict_before(41)
    assert not gaps.overlaps(0, 90)


def test_turn_speed_floor_from_real_review():
    f = FeatureComputer().compute("a", track())
    assert f is not None
    d = RuleDetector()
    assert (
        d.evaluate(replace(f, turn_rate_deg_s=60, reported_speed_ms=1, speed_mismatch_ratio=None))
        is None
    )
    assert (
        d.evaluate(replace(f, turn_rate_deg_s=60, reported_speed_ms=25, speed_mismatch_ratio=None))
        == "rule:impossible_turn"
    )
