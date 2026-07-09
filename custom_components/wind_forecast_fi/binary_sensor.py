"""``binary_sensor.<source>_stale`` — health signal, decoupled from ``available``.

``coordinator.stale`` already tracks elapsed time since
``last_successful_fetch`` independently of HA's own ``last_update_success``
(see ``coordinator.py``'s module docstring) — that decoupling is the whole
point of the never-go-empty contract: a source can be *available* (serving
cached data via ``sensor.py``) while also being *stale* (that data is old).
This entity reports the source's health rather than mirroring the data
sensors' availability, so it stays available even with zero cached points —
"no data" is exactly the kind of problem this sensor exists to surface.

Re-evaluation without a dedicated timer: ``stale`` only changes by the
passage of time relative to ``last_successful_fetch``, so it needs
re-evaluating periodically even without new data. Every coordinator poll
(``update_interval``) already does that for us:

- A healthy or recovering source ticks and notifies listeners normally.
- A source that's failing but still has cached points to serve (the
  never-go-empty contract) returns those points *without raising* —
  ``last_update_success`` therefore stays ``True`` across the failure, so
  ``DataUpdateCoordinator``'s "still failing, still failing" early-return
  (added when ``last_update_success`` is ``False`` both before and after a
  poll) never triggers, and — since ``always_update`` defaults to ``True`` —
  listeners are notified on every one of these polls too.
- A source that has *never* had a successful fetch (no cache at all) does
  hit that early-return on every poll after the first, so it stops notifying
  listeners past that point — but ``stale`` for such a source is already
  pinned ``True`` (``last_successful_fetch`` is ``None``) and cannot become
  anything else, so a missed notification there has no observable effect.

So plain coordinator ticks are sufficient to keep this entity's state
correct; no extra timer is added here.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WindForecastConfigEntry
from .entity import ForecastCoordinator, WindForecastEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WindForecastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the stale binary_sensor for every source/combined subentry."""
    runtime = entry.runtime_data
    for subentry_id, coordinator in runtime.all_coordinators().items():
        subentry = entry.subentries[subentry_id]
        async_add_entities(
            [WindForecastStaleBinarySensor(coordinator, subentry_id, subentry)],
            config_subentry_id=subentry_id,
        )


class WindForecastStaleBinarySensor(WindForecastEntity, BinarySensorEntity):
    """On when the source/combined view hasn't fetched successfully recently."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ForecastCoordinator, subentry_id: str, subentry) -> None:
        super().__init__(coordinator, subentry_id, subentry, "stale")

    @property
    def is_on(self) -> bool:
        return self.coordinator.stale

    @property
    def available(self) -> bool:
        """Always available: this entity reports health, it doesn't depend on it."""
        return True
