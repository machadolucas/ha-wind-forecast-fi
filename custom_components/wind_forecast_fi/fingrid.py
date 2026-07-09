"""Fingrid dataset-245 (wind generation forecast) response parsing + fetch.

The parser is pure; ``async_fetch_fingrid`` is a thin async HTTP client that
may import ``aiohttp`` but never ``homeassistant`` — the coordinator layer
supplies the (HA-shared) session and turns failures into the never-go-empty
contract described in the plan.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aiohttp

from .models import ForecastPoint

# https://data.fingrid.fi/api/datasets/245 — wind power generation forecast.
FINGRID_DATA_URL = "https://data.fingrid.fi/api/datasets/245/data"

# Matches the format the legacy REST sensor used (Jinja's strftime("%z") —
# numeric offset, no colon), now always normalised to UTC.
_PARAM_DT_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
_FETCH_TIMEOUT_S = 30


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


def _format_param_dt(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_PARAM_DT_FORMAT)


async def async_fetch_fingrid(
    session: aiohttp.ClientSession,
    api_key: str,
    *,
    start: datetime,
    end: datetime,
    page_size: int = 1000,
) -> list[ForecastPoint]:
    """Fetch dataset-245 over ``[start, end]`` and return parsed points.

    Raises :class:`FingridAuthError` on 401/403 (bad/missing key) and
    :class:`FingridApiError` for any other non-200 response, network failure,
    timeout, or unparsable body — the coordinator decides what to do with a
    cached fallback, never this module.
    """
    params = {
        "startTime": _format_param_dt(start),
        "endTime": _format_param_dt(end),
        "pageSize": str(page_size),
    }
    headers = {"x-api-key": api_key}
    try:
        async with (
            asyncio.timeout(_FETCH_TIMEOUT_S),
            session.get(FINGRID_DATA_URL, headers=headers, params=params) as resp,
        ):
            if resp.status in (401, 403):
                raise FingridAuthError(f"Fingrid auth failed: HTTP {resp.status}")
            if resp.status != 200:
                raise FingridApiError(f"Fingrid API error: HTTP {resp.status}")
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as err:
                raise FingridApiError(f"Fingrid returned unparsable JSON: {err}") from err
    except FingridApiError:
        raise
    except TimeoutError as err:
        raise FingridApiError("Fingrid request timed out") from err
    except aiohttp.ClientError as err:
        raise FingridApiError(f"Fingrid request failed: {err}") from err

    return parse_fingrid_response(payload)
