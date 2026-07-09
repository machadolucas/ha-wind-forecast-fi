"""The Wind Forecast Finland integration.

Minimal for now: builds one coordinator per source subentry (fingrid/fmi) and
one ``CombinedCoordinator`` per combined subentry, seeds each source from its
Store snapshot, and does a best-effort first refresh. **No entity platforms
yet** — those (plus richer reload/unload wiring) land in a later phase; this
just needs to exist so config-flow tests can set up the entry.

Per the plan's never-go-empty contract: a source with no cache and a dead
network must not block the hub (or a *healthy* sibling source) from setting
up, so this uses ``async_refresh()`` (never raises) rather than
``async_config_entry_first_refresh()`` (which would raise
``ConfigEntryNotReady`` and take the whole hub down with it).

Note: ``.coordinator`` (and transitively ``.fingrid``/``.fmi``) is imported
*inside* ``async_setup_entry`` rather than at module level. ``fingrid.py`` and
``fmi.py`` have their own pure unit tests that load them directly under their
real dotted module name (needed to resolve their internal ``from .models
import ...``), which requires executing this package's ``__init__.py`` first.
A top-level import here of ``.coordinator`` (which itself imports
``.fingrid``/``.fmi``) would make that a circular import — deferring it keeps
those modules loadable standalone, with only ``pytest`` and no Home Assistant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_GAP_FILL,
    CONF_PRIMARY,
    CONF_SECONDARY,
    DEFAULT_GAP_FILL,
    SUBENTRY_TYPE_COMBINED,
    SUBENTRY_TYPE_FINGRID,
    SUBENTRY_TYPE_FMI,
)

if TYPE_CHECKING:
    from .coordinator import CombinedCoordinator, SourceCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class RuntimeData:
    """Live coordinators for one hub entry, keyed by subentry id."""

    sources: dict[str, SourceCoordinator] = field(default_factory=dict)
    combined: dict[str, CombinedCoordinator] = field(default_factory=dict)


type WindForecastConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: WindForecastConfigEntry) -> bool:
    """Set up Wind Forecast Finland from the hub config entry."""
    from .coordinator import CombinedCoordinator, FingridCoordinator, FmiCoordinator

    sources: dict[str, SourceCoordinator] = {}
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type == SUBENTRY_TYPE_FINGRID:
            coordinator: SourceCoordinator = FingridCoordinator(hass, entry, subentry)
        elif subentry.subentry_type == SUBENTRY_TYPE_FMI:
            coordinator = FmiCoordinator(hass, entry, subentry)
        else:
            continue
        await coordinator.async_seed_from_store()
        sources[subentry_id] = coordinator

    # Best-effort: async_refresh() (unlike async_config_entry_first_refresh())
    # never raises, so one dead source can't block hub setup or its siblings.
    for coordinator in sources.values():
        await coordinator.async_refresh()

    combined: dict[str, CombinedCoordinator] = {}
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_COMBINED:
            continue
        primary = sources.get(subentry.data.get(CONF_PRIMARY))
        if primary is None:
            _LOGGER.warning(
                "Combined subentry %s has no valid primary source configured; skipping",
                subentry_id,
            )
            continue
        secondary_id = subentry.data.get(CONF_SECONDARY)
        secondary = sources.get(secondary_id) if secondary_id else None
        gap_fill = subentry.data.get(CONF_GAP_FILL, DEFAULT_GAP_FILL)
        combined_coordinator = CombinedCoordinator(
            hass, entry, subentry, primary, secondary, gap_fill
        )
        combined_coordinator.start()
        combined[subentry_id] = combined_coordinator

    entry.runtime_data = RuntimeData(sources=sources, combined=combined)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WindForecastConfigEntry) -> bool:
    """Unload the hub config entry: stop every coordinator."""
    runtime: RuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is None:
        return True
    for coordinator in runtime.combined.values():
        await coordinator.async_shutdown()
    for coordinator in runtime.sources.values():
        await coordinator.async_shutdown()
    return True
