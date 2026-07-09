"""Shared base entity for the sensor/binary_sensor platforms.

One HA device per subentry — a source (Fingrid/FMI) or a combined view —
matching one coordinator per subentry in ``__init__.py``. Works with either
``SourceCoordinator`` or ``CombinedCoordinator`` since this base (and its
subclasses) only touches their shared read-only surface: ``.points``,
``.stale``, ``.last_successful_fetch`` and ``.source_kind`` (see
``coordinator.py``).
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CombinedCoordinator, SourceCoordinator

ForecastCoordinator = SourceCoordinator | CombinedCoordinator


class WindForecastEntity(CoordinatorEntity):
    """Base entity for one source/combined subentry (= one device)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ForecastCoordinator,
        subentry_id: str,
        subentry: ConfigSubentry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=subentry.title,
            manufacturer="Wind Forecast Finland",
            entry_type=DeviceEntryType.SERVICE,
        )
