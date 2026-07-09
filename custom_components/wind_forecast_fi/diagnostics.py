"""Diagnostics for the Wind Forecast Finland hub.

Redacts the Fingrid API key; surfaces every source/combined subentry's
config and health (fetch recency, staleness, failure count, cached horizon)
— enough to diagnose a stuck/stale source without a live HA session.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import WindForecastConfigEntry
from .const import CONF_API_KEY

_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WindForecastConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the hub config entry."""
    runtime = entry.runtime_data
    sources: dict[str, Any] = {}
    for subentry_id, coordinator in runtime.all_coordinators().items():
        subentry = entry.subentries[subentry_id]
        points = coordinator.points
        sources[subentry_id] = {
            "type": subentry.subentry_type,
            "title": subentry.title,
            "config": async_redact_data(dict(subentry.data), _REDACT),
            "health": {
                "last_successful_fetch": (
                    coordinator.last_successful_fetch.isoformat()
                    if coordinator.last_successful_fetch is not None
                    else None
                ),
                "stale": coordinator.stale,
                "failures": coordinator.failures,
                "n_points": len(points),
                "horizon_start": points[0].start.isoformat() if points else None,
                "horizon_end": points[-1].end.isoformat() if points else None,
            },
        }

    return {
        "hub": {"name": entry.data.get("name")},
        "sources": sources,
    }
