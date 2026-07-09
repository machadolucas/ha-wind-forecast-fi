"""Unit tests for the pure forecast model (no Home Assistant).

Loaded standalone via ``importlib`` so these run without importing the
HA-bound package. Mirrors the ``test_price_source.py`` pattern in
ha-load-scheduler. ``models.py`` has zero internal deps, so an arbitrary
sys.modules name is safe here (unlike fingrid.py/fmi.py, see their tests).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, timedelta

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "wind_forecast_fi"
    / "models.py"
)
_spec = importlib.util.spec_from_file_location("wf_models", _PATH)
models = importlib.util.module_from_spec(_spec)
sys.modules["wf_models"] = models
_spec.loader.exec_module(models)

ForecastPoint = models.ForecastPoint


def _pt(start: datetime, end: datetime, value: float) -> ForecastPoint:
    return ForecastPoint(start=start, end=end, value_mw=value)


T0 = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# merge_forecasts
# --------------------------------------------------------------------------- #


def test_merge_no_gap_fill_returns_primary_only_sorted():
    primary = [
        _pt(T0 + timedelta(hours=1), T0 + timedelta(hours=2), 2.0),
        _pt(T0, T0 + timedelta(hours=1), 1.0),
    ]
    secondary = [_pt(T0 + timedelta(hours=5), T0 + timedelta(hours=6), 9.0)]
    merged = models.merge_forecasts(primary, secondary, gap_fill=False)
    assert [p.value_mw for p in merged] == [1.0, 2.0]
    assert [p.start for p in merged] == sorted(p.start for p in merged)


def test_merge_gap_fill_adds_non_overlapping_secondary():
    primary = [_pt(T0, T0 + timedelta(hours=1), 1.0)]
    secondary = [
        _pt(T0, T0 + timedelta(hours=1), 99.0),  # overlaps primary -> excluded
        _pt(T0 + timedelta(hours=1), T0 + timedelta(hours=2), 2.0),  # true gap -> kept
    ]
    merged = models.merge_forecasts(primary, secondary, gap_fill=True)
    assert len(merged) == 2
    assert merged[0].value_mw == 1.0
    assert merged[1].value_mw == 2.0


def test_merge_gap_fill_mixed_resolutions_15min_primary_hourly_secondary():
    # Fingrid-style 15-min primary covering the first half-hour; FMI-style
    # hourly secondary covering two hours. Only the second (non-overlapping)
    # secondary hour should be kept.
    primary = [
        _pt(T0, T0 + timedelta(minutes=15), 1.0),
        _pt(T0 + timedelta(minutes=15), T0 + timedelta(minutes=30), 1.1),
    ]
    secondary = [
        _pt(T0, T0 + timedelta(hours=1), 5.0),  # overlaps both primary buckets
        _pt(T0 + timedelta(hours=1), T0 + timedelta(hours=2), 6.0),  # true gap
    ]
    merged = models.merge_forecasts(primary, secondary, gap_fill=True)
    assert len(merged) == 3
    assert merged[-1].value_mw == 6.0


def test_merge_empty_primary_gap_fill_on_returns_secondary():
    secondary = [_pt(T0, T0 + timedelta(hours=1), 3.0)]
    merged = models.merge_forecasts([], secondary, gap_fill=True)
    assert merged == secondary


def test_merge_empty_primary_gap_fill_off_returns_empty():
    secondary = [_pt(T0, T0 + timedelta(hours=1), 3.0)]
    merged = models.merge_forecasts([], secondary, gap_fill=False)
    assert merged == []


# --------------------------------------------------------------------------- #
# current_value
# --------------------------------------------------------------------------- #


def test_current_value_in_bucket():
    points = [_pt(T0, T0 + timedelta(hours=1), 5.0)]
    assert models.current_value(points, T0 + timedelta(minutes=30)) == 5.0


def test_current_value_future_fallback_on_gap():
    points = [_pt(T0 + timedelta(hours=2), T0 + timedelta(hours=3), 7.0)]
    # now falls in a gap before any bucket starts -> earliest future bucket
    assert models.current_value(points, T0) == 7.0


def test_current_value_all_past_returns_none():
    points = [_pt(T0 - timedelta(hours=2), T0 - timedelta(hours=1), 5.0)]
    assert models.current_value(points, T0) is None


def test_current_value_empty_returns_none():
    assert models.current_value([], T0) is None


# --------------------------------------------------------------------------- #
# next_24h_stats
# --------------------------------------------------------------------------- #


def test_next_24h_stats_avg_min_max_times():
    points = [
        _pt(T0, T0 + timedelta(hours=1), 10.0),
        _pt(T0 + timedelta(hours=1), T0 + timedelta(hours=2), 2.0),
        _pt(T0 + timedelta(hours=2), T0 + timedelta(hours=3), 6.0),
    ]
    stats = models.next_24h_stats(points, T0)
    assert stats is not None
    assert stats.avg_mw == (10.0 + 2.0 + 6.0) / 3
    assert stats.min_mw == 2.0
    assert stats.min_time == T0 + timedelta(hours=1)
    assert stats.max_mw == 10.0
    assert stats.max_time == T0


def test_next_24h_stats_excludes_outside_window():
    points = [
        _pt(T0 - timedelta(hours=2), T0 - timedelta(hours=1), 100.0),  # past, excluded
        _pt(T0, T0 + timedelta(hours=1), 5.0),
        _pt(T0 + timedelta(hours=30), T0 + timedelta(hours=31), 100.0),  # beyond 24h
    ]
    stats = models.next_24h_stats(points, T0)
    assert stats.avg_mw == 5.0


def test_next_24h_stats_empty_returns_none():
    assert models.next_24h_stats([], T0) is None
    # nothing overlapping the window either
    points = [_pt(T0 - timedelta(hours=5), T0 - timedelta(hours=4), 1.0)]
    assert models.next_24h_stats(points, T0) is None


# --------------------------------------------------------------------------- #
# trim_past
# --------------------------------------------------------------------------- #


def test_trim_past_drops_old_points():
    points = [
        _pt(T0 - timedelta(hours=10), T0 - timedelta(hours=9), 1.0),
        _pt(T0 - timedelta(hours=1), T0, 2.0),
        _pt(T0, T0 + timedelta(hours=1), 3.0),
    ]
    trimmed = models.trim_past(points, T0, timedelta(hours=2))
    assert [p.value_mw for p in trimmed] == [2.0, 3.0]


# --------------------------------------------------------------------------- #
# storage round-trip
# --------------------------------------------------------------------------- #


def test_storage_round_trip():
    points = [
        _pt(T0, T0 + timedelta(hours=1), 5.5),
        _pt(T0 + timedelta(hours=1), T0 + timedelta(hours=2), 6.5),
    ]
    raw = models.points_to_storage(points)
    restored = models.points_from_storage(raw)
    assert restored == points


def test_storage_from_malformed_rows_skips_them():
    raw = [
        {"start": T0.isoformat(), "end": (T0 + timedelta(hours=1)).isoformat(), "value_mw": 1.0},
        {"start": "not-a-date", "end": "also-not", "value_mw": 1.0},
        {"start": T0.isoformat()},  # missing keys
    ]
    restored = models.points_from_storage(raw)
    assert len(restored) == 1


# --------------------------------------------------------------------------- #
# to_attributes — exact-shape contract test
# --------------------------------------------------------------------------- #


def test_to_attributes_exact_shape():
    points = [
        _pt(T0, T0 + timedelta(hours=1), 5.0),
        _pt(T0 + timedelta(hours=1), T0 + timedelta(hours=2), 6.0),
    ]
    attrs = models.to_attributes(points, source="fingrid", last_successful_fetch=T0, stale=False)
    assert set(attrs.keys()) == {
        "forecast",
        "unit",
        "source",
        "last_successful_fetch",
        "stale",
        "horizon_start",
        "horizon_end",
    }
    assert attrs["forecast"] == [
        {"start": T0.isoformat(), "end": (T0 + timedelta(hours=1)).isoformat(), "value": 5.0},
        {
            "start": (T0 + timedelta(hours=1)).isoformat(),
            "end": (T0 + timedelta(hours=2)).isoformat(),
            "value": 6.0,
        },
    ]
    assert attrs["unit"] == "MW"
    assert attrs["source"] == "fingrid"
    assert attrs["last_successful_fetch"] == T0.isoformat()
    assert attrs["stale"] is False
    assert attrs["horizon_start"] == T0.isoformat()
    assert attrs["horizon_end"] == (T0 + timedelta(hours=2)).isoformat()


def test_to_attributes_empty_points_and_no_fetch():
    attrs = models.to_attributes([], source="fmi", last_successful_fetch=None, stale=True)
    assert attrs["forecast"] == []
    assert attrs["last_successful_fetch"] is None
    assert attrs["stale"] is True
    assert attrs["horizon_start"] is None
    assert attrs["horizon_end"] is None
