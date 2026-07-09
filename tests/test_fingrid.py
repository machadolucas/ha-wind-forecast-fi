"""Unit tests for Fingrid response parsing (no Home Assistant).

fingrid.py has an internal relative import (``from .models import
ForecastPoint``), so — unlike the fully standalone test_models.py — it is
loaded under its *real* dotted module name. That makes Python resolve
``.models`` via the normal namespace-package lookup (repo root is on
``sys.path`` via conftest.py) instead of raising "no known parent package".
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import UTC, datetime

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PATH = _ROOT / "custom_components" / "wind_forecast_fi" / "fingrid.py"
_MODNAME = "custom_components.wind_forecast_fi.fingrid"
_spec = importlib.util.spec_from_file_location(_MODNAME, _PATH)
fingrid = importlib.util.module_from_spec(_spec)
sys.modules[_MODNAME] = fingrid
_spec.loader.exec_module(fingrid)

_FIXTURE = json.loads((_ROOT / "tests" / "fixtures" / "fingrid_sample.json").read_text())


def test_parses_real_fixture_count_and_ascending_order():
    points = fingrid.parse_fingrid_response(_FIXTURE)
    assert len(points) == 8
    assert [p.start for p in points] == sorted(p.start for p in points)
    # fixture is descending on the wire; first point after sort is the earliest.
    assert points[0].start == datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    assert points[-1].end == datetime(2026, 7, 9, 14, 0, tzinfo=UTC)


def test_values_pass_through_unchanged_mw():
    points = fingrid.parse_fingrid_response(_FIXTURE)
    by_start = {p.start: p.value_mw for p in points}
    assert by_start[datetime(2026, 7, 9, 12, 0, tzinfo=UTC)] == 1013.3
    assert by_start[datetime(2026, 7, 9, 13, 45, tzinfo=UTC)] == 913.1


def test_bucket_width_is_15_minutes():
    points = fingrid.parse_fingrid_response(_FIXTURE)
    for p in points:
        assert (p.end - p.start).total_seconds() == 15 * 60


def test_empty_data_list_returns_empty():
    assert fingrid.parse_fingrid_response({"data": []}) == []


def test_missing_data_key_returns_empty():
    assert fingrid.parse_fingrid_response({}) == []


def test_non_dict_payload_returns_empty():
    assert fingrid.parse_fingrid_response({"data": "not-a-list"}) == []


def test_malformed_rows_are_skipped_not_raised():
    payload = {
        "data": [
            {
                "startTime": "2026-07-09T12:00:00.000Z",
                "endTime": "2026-07-09T12:15:00.000Z",
                "value": 100.0,
            },
            {"startTime": "not-a-date", "endTime": "2026-07-09T12:30:00.000Z", "value": 1.0},
            {"startTime": "2026-07-09T12:15:00.000Z"},  # missing endTime/value
            "not-a-dict",
        ]
    }
    points = fingrid.parse_fingrid_response(payload)
    assert len(points) == 1
    assert points[0].value_mw == 100.0
