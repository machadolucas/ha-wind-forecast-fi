"""Fingrid dataset-245 (wind generation forecast) response parsing.

Pure parsing only — the HTTP client (``FingridClient``) is added in the
coordinator phase; this module just needs to be importable by it, hence the
exception classes and URL constant live here already.
"""

from __future__ import annotations

from datetime import datetime

from .models import ForecastPoint

# https://data.fingrid.fi/api/datasets/245 — wind power generation forecast.
FINGRID_DATA_URL = "https://data.fingrid.fi/api/datasets/245/data"


class FingridApiError(Exception):
    """Raised on a non-2xx Fingrid response that isn't an auth failure."""


class FingridAuthError(FingridApiError):
    """Raised on 401/403 — bad or missing ``x-api-key``."""


def _parse_dt(value: str) -> datetime:
    # Fingrid emits "...000Z" (millisecond + Zulu suffix); fromisoformat handles
    # both the "Z" suffix and numeric offsets natively on 3.11+.
    return datetime.fromisoformat(value)


def parse_fingrid_response(payload: dict) -> list[ForecastPoint]:
    """Parse a Fingrid ``datasets/245/data`` payload into ascending ForecastPoints.

    Values are already MW. The API returns pages in descending time order;
    malformed rows are skipped rather than raised, per the plan's "never crash
    on one bad point" rule.
    """
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []

    points: list[ForecastPoint] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            points.append(
                ForecastPoint(
                    start=_parse_dt(row["startTime"]),
                    end=_parse_dt(row["endTime"]),
                    value_mw=float(row["value"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    points.sort(key=lambda p: p.start)
    return points
