"""FMI wind-power forecast CDN response parsing.

Pure parsing only — the HTTP client (``FmiClient``) is added in the
coordinator phase; this module just needs to be importable by it, hence the
exception class and URL constant live here already.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import median

from .models import ForecastPoint

FMI_FORECAST_URL = (
    "https://cdn.fmi.fi/products/renewable-energy-forecasts/wind/windpower_en_latest.json"
)

_FALLBACK_STEP = timedelta(hours=1)


class FmiApiError(Exception):
    """Raised on a non-2xx or unparseable FMI response."""


def _epoch_ms_to_dt(value: float) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def parse_fmi_response(payload: dict) -> list[ForecastPoint]:
    """Parse the FMI Highcharts-style payload into ascending ForecastPoints.

    ``series[0].data`` is ``[epoch_ms, gw]`` pairs, hourly, start-only — the
    end of each bucket is synthesised from the gap to the next point (the
    last bucket uses the median gap, i.e. the typical hourly step, falling
    back to 1h when there's only a single point to work with). Values are
    GW; ×1000 converts to the canonical MW. Malformed entries are skipped.
    """
    series = payload.get("series") if isinstance(payload, dict) else None
    if not isinstance(series, list) or not series:
        return []
    data = series[0].get("data") if isinstance(series[0], dict) else None
    if not isinstance(data, list):
        return []

    parsed: list[tuple[datetime, float]] = []
    for entry in data:
        if not isinstance(entry, list | tuple) or len(entry) != 2:
            continue
        epoch_ms, value_gw = entry
        try:
            start = _epoch_ms_to_dt(float(epoch_ms))
            value_mw = float(value_gw) * 1000
        except (TypeError, ValueError):
            continue
        parsed.append((start, value_mw))

    parsed.sort(key=lambda item: item[0])
    if not parsed:
        return []

    starts = [p[0] for p in parsed]
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False) if b > a]
    typical_step = median(gaps) if gaps else _FALLBACK_STEP

    points: list[ForecastPoint] = []
    for i, (start, value_mw) in enumerate(parsed):
        end = starts[i + 1] if i + 1 < len(starts) else start + typical_step
        points.append(ForecastPoint(start=start, end=end, value_mw=value_mw))
    return points
