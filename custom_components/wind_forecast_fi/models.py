"""Pure forecast data model: points, merge, current/next-24h stats, (de)serialisation.

Home-Assistant-free by design (mirrors ``price_source.py`` in ha-load-scheduler
and ``models.py`` in ha-load-need-predictor): every function here operates on
plain ``ForecastPoint``/``dict``/``datetime`` values, so it is unit-testable
under plain pytest. The coordinator layer (later phase) is the only place that
touches Home Assistant.

Buckets are half-open ``[start, end)`` in MW, tz-aware. Fingrid publishes
15-minute buckets, FMI hourly — ``merge_forecasts`` is written to not assume a
common resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ForecastPoint:
    """One forecast bucket: half-open ``[start, end)``, value in MW."""

    start: datetime
    end: datetime
    value_mw: float


@dataclass(frozen=True)
class Next24hStats:
    """Aggregate over the next-24h window, feeding the ``_next_24h`` sensor."""

    avg_mw: float
    min_mw: float
    min_time: datetime
    max_mw: float
    max_time: datetime


def merge_forecasts(
    primary: list[ForecastPoint],
    secondary: list[ForecastPoint],
    gap_fill: bool,
) -> list[ForecastPoint]:
    """Merge two point lists: primary wins, secondary only fills true gaps.

    Not blending — the two sources' methodologies disagree, so averaging would
    match neither. A secondary point is kept only when ``gap_fill`` is set and
    its ``[start, end)`` bucket does not overlap *any* primary bucket; this is
    resolution-agnostic (a secondary point can straddle several primary
    buckets, or vice versa) since overlap is a plain interval test, not an
    equality check on ``start``.
    """
    if not gap_fill:
        return sorted(primary, key=lambda p: p.start)

    def overlaps_any(point: ForecastPoint, others: list[ForecastPoint]) -> bool:
        return any(point.start < o.end and o.start < point.end for o in others)

    fill = [p for p in secondary if not overlaps_any(p, primary)]
    return sorted([*primary, *fill], key=lambda p: p.start)


def current_value(points: list[ForecastPoint], now: datetime) -> float | None:
    """Value of the bucket containing ``now``; else the earliest future bucket.

    Mirrors the plan's state semantics: current-hour value, falling back to
    the next upcoming bucket if the current one is missing (a gap), and
    ``None`` only when nothing with ``end > now`` exists at all.
    """
    for p in points:
        if p.start <= now < p.end:
            return p.value_mw
    future = [p for p in points if p.end > now]
    if not future:
        return None
    return min(future, key=lambda p: p.start).value_mw


def next_24h_stats(points: list[ForecastPoint], now: datetime) -> Next24hStats | None:
    """Avg/min/max over points overlapping ``[now, now + 24h)``; ``None`` if none."""
    horizon_end = now + timedelta(hours=24)
    window = [p for p in points if p.start < horizon_end and p.end > now]
    if not window:
        return None
    values = [p.value_mw for p in window]
    min_point = min(window, key=lambda p: p.value_mw)
    max_point = max(window, key=lambda p: p.value_mw)
    return Next24hStats(
        avg_mw=sum(values) / len(values),
        min_mw=min_point.value_mw,
        min_time=min_point.start,
        max_mw=max_point.value_mw,
        max_time=max_point.start,
    )


def trim_past(
    points: list[ForecastPoint], now: datetime, retention: timedelta
) -> list[ForecastPoint]:
    """Drop points whose bucket ended before ``now - retention``.

    Keeps the Store snapshot from growing unbounded while still retaining a
    short lookback window for diagnostics/graphing.
    """
    cutoff = now - retention
    return [p for p in points if p.end > cutoff]


def to_attributes(
    points: list[ForecastPoint],
    *,
    source: str,
    last_successful_fetch: datetime | None,
    stale: bool,
) -> dict:
    """Build the canonical attribute dict — the single source of this shape.

    Exact schema from the plan; kept in one place so it's contract-tested and
    every entity (fingrid/fmi/combined) renders it identically.
    """
    ordered = sorted(points, key=lambda p: p.start)
    return {
        "forecast": [
            {"start": p.start.isoformat(), "end": p.end.isoformat(), "value": p.value_mw}
            for p in ordered
        ],
        "unit": "MW",
        "source": source,
        "last_successful_fetch": (
            last_successful_fetch.isoformat() if last_successful_fetch is not None else None
        ),
        "stale": stale,
        "horizon_start": ordered[0].start.isoformat() if ordered else None,
        "horizon_end": ordered[-1].end.isoformat() if ordered else None,
    }


def points_to_storage(points: list[ForecastPoint]) -> list[dict]:
    """Serialise points for the ``Store`` snapshot (ISO strings, JSON-safe)."""
    return [
        {"start": p.start.isoformat(), "end": p.end.isoformat(), "value_mw": p.value_mw}
        for p in points
    ]


def points_from_storage(raw: list[dict]) -> list[ForecastPoint]:
    """Inverse of :func:`points_to_storage`. Malformed rows are skipped."""
    points: list[ForecastPoint] = []
    for row in raw:
        try:
            points.append(
                ForecastPoint(
                    start=datetime.fromisoformat(row["start"]),
                    end=datetime.fromisoformat(row["end"]),
                    value_mw=float(row["value_mw"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return points
