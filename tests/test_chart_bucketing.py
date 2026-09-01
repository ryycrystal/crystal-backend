import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.api import _bucket_median_by_time, _sample_evenly_by_time

TS = lambda p: int(p["timestamp"])  # noqa: E731
VAL = lambda p: float(p["v"])  # noqa: E731


def _series(n, start=1_000_000, step=30, value=100.0):
    return [{"timestamp": start + i * step, "v": value} for i in range(n)]


def test_a_lone_spike_never_represents_its_bucket():
    pts = _series(600)
    pts[301]["v"] = 5.0  # one bad sample among 600
    out = _bucket_median_by_time(pts, 48, TS, VAL)
    assert len(out) <= 48
    assert all(p["v"] == 100.0 for p in out), "an outlier leaked into the rendered series"


def test_the_old_picker_could_surface_that_same_spike():
    # documents why the median bucketing exists: whichever sample the nearest to
    # target search lands on becomes the whole bucket, outlier or not
    clean = _series(600)
    landed_on = {int(p["timestamp"]) for p in _sample_evenly_by_time(clean, 48, TS)}

    pts = _series(600)
    spiked = next(p for p in pts if int(p["timestamp"]) in landed_on and p is not pts[-1])
    spiked["v"] = 5.0

    assert any(p["v"] == 5.0 for p in _sample_evenly_by_time(pts, 48, TS))
    assert all(p["v"] == 100.0 for p in _bucket_median_by_time(pts, 48, TS, VAL))


def test_shape_is_stable_as_the_rolling_window_advances():
    base = _series(600)
    base[301]["v"] = 5.0
    shapes = set()
    for drop in range(0, 12):
        # simulate the window sliding forward one sample at a time
        window = base[drop:]
        out = _bucket_median_by_time(window, 48, TS, VAL)
        shapes.add(tuple(round(float(p["v"]), 6) for p in out))
    assert len(shapes) == 1, "the curve changed shape as the window rolled"


def test_real_level_shifts_survive():
    pts = _series(600, value=100.0)
    for p in pts[300:]:
        p["v"] = 200.0
    out = _bucket_median_by_time(pts, 48, TS, VAL)
    assert any(p["v"] == 100.0 for p in out)
    assert any(p["v"] == 200.0 for p in out)


def test_the_series_ends_on_the_newest_sample():
    pts = _series(600)
    pts[-1]["v"] = 123.5
    out = _bucket_median_by_time(pts, 48, TS, VAL)
    assert out[-1]["timestamp"] == pts[-1]["timestamp"]
    assert out[-1]["v"] == 123.5


def test_small_inputs_pass_through_untouched():
    pts = _series(10)
    assert _bucket_median_by_time(pts, 48, TS, VAL) == pts
    assert _bucket_median_by_time([], 48, TS, VAL) == []


def test_output_stays_sorted_by_time():
    pts = _series(600)
    out = _bucket_median_by_time(pts, 48, TS, VAL)
    assert [p["timestamp"] for p in out] == sorted(p["timestamp"] for p in out)
