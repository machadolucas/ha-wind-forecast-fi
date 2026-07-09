"""The Wind Forecast Finland integration.

Builds one coordinator per source subentry (fingrid/fmi) and one
``CombinedCoordinator`` per combined subentry, seeds each source from its
Store snapshot, does a best-effort first refresh, then forwards the
``sensor``/``binary_sensor`` platforms (one device per subentry — see
``entity.py``).

Per the plan's never-go-empty contract: a source with no cache and a dead
network must not block the hub (or a *healthy* sibling source) from setting
up, so this uses ``async_refresh()`` (never raises) rather than
``async_config_entry_first_refresh()`` (which would raise
``ConfigEntryNotReady`` and take the whole hub down with it).

Subentry add/remove/update all go through a full reload (``add_update_listener``
below — mirrors ``ha-load-scheduler``): HA fires the entry's update listeners
on any subentry mutation (``ConfigEntries._async_update_entry``), and a
reload is simplest way to pick up an added/removed/edited source or combined
view consistently. One consequence: this integration does **not** clean up a
removed source's ``Store`` file (``ha-load-scheduler``/``ha-load-need-predictor``
don't bother with the equivalent either) — the reload listener only sees the
*new* subentry set, not a diff against the old one, so knowing which subentry
was actually removed (as opposed to renamed/edited) would need extra state
tracking that isn't justified by an orphaned few-KB cache file; it's simply
never read again after its subentry is gone.

Note: ``.coordinator`` (and transitively ``.fingrid``/``.fmi``) is imported
*inside* ``async_setup_entry`` rather than at module level. ``fingrid.py`` and
``fmi.py`` have their own pure unit tests that load them directly under their
real dotted module name (needed to resolve their internal ``from .models
import ...``), which requires executing this package's ``__init__.py`` first.
A top-level import here of ``.coordinator`` (which itself imports
``.fingrid``/``.fmi``) would make that a circular import — deferring it keeps
those modules loadable standalone, with only ``pytest`` and no Home Assistant.
``entity.py``/``sensor.py``/``binary_sensor.py``/``diagnostics.py`` don't have
that constraint (HA imports them lazily, well after this module has finished
loading), so they import ``.coordinator`` normally at module level.
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
    PLATFORMS,
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

    def all_coordinators(self) -> dict[str, SourceCoordinator | CombinedCoordinator]:
        """Every source + combined coordinator, keyed by subentry id.

        The single iteration point platforms use to add their per-subentry
        entities (``sensor.py``/``binary_sensor.py``) — sources and combined
        views get identical entities, just from a different coordinator type.
        """
        return {**self.sources, **self.combined}


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

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WindForecastConfigEntry) -> bool:
    """Unload the hub config entry: platforms first, then every coordinator."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime: RuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is not None:
        for coordinator in runtime.combined.values():
            await coordinator.async_shutdown()
        for coordinator in runtime.sources.values():
            await coordinator.async_shutdown()
    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: WindForecastConfigEntry) -> None:
    """Reload on hub/subentry changes (picks up an added/removed/edited source
    or combined view — see the module docstring for why this is a full reload
    rather than a targeted diff).
    """
    await hass.config_entries.async_reload(entry.entry_id)
