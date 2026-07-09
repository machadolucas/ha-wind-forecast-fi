"""Unit tests for FMI response parsing (no Home Assistant).

Same "load under its real dotted name" trick as test_fingrid.py, since fmi.py
also does ``from .models import ForecastPoint``.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import UTC, datetime, timedelta

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PATH = _ROOT / "custom_components" / "wind_forecast_fi" / "fmi.py"
_MODNAME = "custom_components.wind_forecast_fi.fmi"
_spec = importlib.util.spec_from_file_location(_MODNAME, _PATH)
fmi = importlib.util.module_from_spec(_spec)
sys.modules[_MODNAME] = fmi
_spec.loader.exec_module(fmi)

_FIXTURE = json.loads((_ROOT / "tests" / "fixtures" / "fmi_sample.json").read_text())


def test_parses_real_fixture_count_and_ascending_order():
    points = fmi.parse_fmi_response(_FIXTURE)
    assert len(points) == 8
    assert [p.start for p in points] == sorted(p.start for p in points)
    assert points[0].start == datetime(2026, 7, 9, 11, 0, tzinfo=UTC)


def test_unit_conversion_gw_to_mw():
    points = fmi.parse_fmi_response(_FIXTURE)
    # first fixture point is 1.186 GW -> 1186.0 MW
    assert points[0].value_mw == 1186.0


def test_synthesized_ends_are_one_hour():
    points = fmi.parse_fmi_response(_FIXTURE)
    for p in points:
        assert (p.end - p.start) == timedelta(hours=1)
    # ends chain to the next start except the (synthesised) last one
    for a, b in zip(points, points[1:], strict=False):
        assert a.end == b.start


def test_single_point_falls_back_to_one_hour():
    t0 = 1_800_000_000_000.0  # arbitrary epoch ms
    payload = {"series": [{"name": "x", "data": [[t0, 1.0]]}]}
    points = fmi.parse_fmi_response(payload)
    assert len(points) == 1
    assert points[0].end - points[0].start == timedelta(hours=1)


def test_missing_series_returns_empty():
    assert fmi.parse_fmi_response({}) == []
    assert fmi.parse_fmi_response({"series": []}) == []


def test_missing_data_returns_empty():
    assert fmi.parse_fmi_response({"series": [{"name": "x"}]}) == []


def test_all_entries_malformed_returns_empty():
    payload = {"series": [{"name": "x", "data": [["bad", 1.0], [1.0, "bad"]]}]}
    assert fmi.parse_fmi_response(payload) == []


def test_malformed_entries_are_skipped_not_raised():
    t0 = 1_800_000_000_000.0
    payload = {
        "series": [
            {
                "name": "x",
                "data": [
                    [t0, 1.0],
                    [t0 + 3_600_000, "not-a-number"],
                    ["not-a-number", 2.0],
                    [t0 + 7_200_000],  # wrong length
                    [t0 + 10_800_000, 2.0],
                ],
            }
        ]
    }
    points = fmi.parse_fmi_response(payload)
    assert len(points) == 2
    assert points[0].value_mw == 1000.0
    assert points[1].value_mw == 2000.0


def test_median_step_used_for_last_point_with_irregular_gaps():
    # Two normal 1h gaps then a 2h gap before the last point: median of
    # [1h, 1h, 2h] is 1h, so the synthesised final bucket should be 1h wide.
    t0 = 1_800_000_000_000.0
    hour_ms = 3_600_000
    payload = {
        "series": [
            {
                "name": "x",
                "data": [
                    [t0, 1.0],
                    [t0 + hour_ms, 1.1],
                    [t0 + 2 * hour_ms, 1.2],
                    [t0 + 4 * hour_ms, 1.3],  # 2h gap from previous
                ],
            }
        ]
    }
    points = fmi.parse_fmi_response(payload)
    assert points[-1].end - points[-1].start == timedelta(hours=1)
