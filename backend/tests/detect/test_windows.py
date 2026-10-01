from dataclasses import replace

from app.detect.windows import Sample, TrackWindows
from app.models import StateVector


def sample(t: float = 0) -> Sample:
    return Sample(t, t, 37.0, -122.0, 10000, 10050, 200, 90, 0, False, "bay_area")


def test_order_position_gap_readiness_and_eviction():
    windows = TrackWindows()
    for i in range(12):
        assert windows.append_sample("abc123", sample(i * 9))
        assert windows.is_ready("abc123") == (i >= 4)
    assert len(windows.get("abc123")) == 10
    assert not windows.append_sample("abc123", sample(99))
    assert not windows.append_sample("abc123", sample(98))
    assert windows.append_sample("abc123", sample(189))
    assert len(windows.get("abc123")) == 1
    assert not windows.ready
    windows.append_sample("other", sample(200))
    assert windows.evict_before(190) == 1
    assert len(windows) == 1


def test_span_trimming_and_gap_boundary():
    windows = TrackWindows(gap_s=100)
    for t in (0, 50, 100, 150, 200):
        windows.append_sample("a", sample(t))
    assert windows.get("a")[0].last_contact == 50
    assert windows.append_sample("a", sample(300))
    assert len(windows.get("a")) == 3


def test_reject_positionless_without_advancing_clock():
    windows = TrackWindows()
    sv = StateVector.model_construct(icao24="a", last_contact=10, latitude=None, longitude=-122)
    assert not windows.append(sv, "bay_area")
    assert len(windows) == 0
    assert windows.append_sample("a", sample(9))
    assert not windows.append_sample("a", replace(sample(11), lat=float("nan")))
