"""``sensor.<source>_forecast`` / ``..._next_24h`` — canonical schema + derived stats.

Both entities are added per subentry (source or combined) by
``async_setup_entry``, iterating ``RuntimeData.all_coordinators()`` on
``entry.runtime_data`` — one device (and one pair of sensors) per subentry.

Refresh-lag tradeoff (deliberate, no extra timer added for it): the *state*
of both sensors depends on wall-clock time (which hourly/next-24h bucket
``now`` currently falls in), but the only thing that re-evaluates it is a
coordinator tick (``update_interval``, default 15 min). That means a state
can lag up to one poll interval after crossing into a new bucket — e.g. the
`forecast` sensor might show the value for 12:00-13:00 for a few minutes
after 13:00 ticks over. The legacy REST template sensors this integration
replaces had the exact same lag (they were also poll-driven), so this is not
a regression, and adding a dedicated ``async_track_time_change``/interval
timer per entity purely to shave off that lag isn't worth the extra
complexity.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import WindForecastConfigEntry, models
from .entity import ForecastCoordinator, WindForecastEntity
from .models import Next24hStats


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WindForecastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the forecast + next-24h sensors for every source/combined subentry."""
    runtime = entry.runtime_data
    for subentry_id, coordinator in runtime.all_coordinators().items():
        subentry = entry.subentries[subentry_id]
        async_add_entities(
            [
                WindForecastSensor(coordinator, subentry_id, subentry),
                WindForecastNext24hSensor(coordinator, subentry_id, subentry),
            ],
            config_subentry_id=subentry_id,
        )


class WindForecastSensor(WindForecastEntity, SensorEntity):
    """Main sensor: state = current-hour value (MW); attrs = canonical schema."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.MEGA_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The bulky per-bucket list doesn't belong in the recorder — see
    # models.to_attributes(); the state itself keeps history just fine.
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(self, coordinator: ForecastCoordinator, subentry_id: str, subentry) -> None:
        super().__init__(coordinator, subentry_id, subentry, "forecast")

    @property
    def native_value(self) -> float | None:
        value = models.current_value(self.coordinator.points, dt_util.utcnow())
        return round(value, 1) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        return models.to_attributes(
            self.coordinator.points,
            source=self.coordinator.source_kind,
            last_successful_fetch=self.coordinator.last_successful_fetch,
            stale=self.coordinator.stale,
        )

    @property
    def available(self) -> bool:
        """True iff at least one cached bucket hasn't ended yet.

        Deliberately not the inherited ``CoordinatorEntity.available``
        (``coordinator.last_update_success``): under the never-go-empty
        contract, a coordinator that's currently failing but still serving
        cached points must keep showing available data, while one that has
        never had a successful fetch (so ``points`` is empty) must show
        unavailable even though ``last_update_success`` may technically be
        ``True`` for an empty result.
        """
        now = dt_util.utcnow()
        return any(point.end > now for point in self.coordinator.points)


class WindForecastNext24hSensor(WindForecastEntity, SensorEntity):
    """State = avg MW over the next 24h; attrs = min/max value + their times."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.MEGA_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ForecastCoordinator, subentry_id: str, subentry) -> None:
        super().__init__(coordinator, subentry_id, subentry, "next_24h")

    @property
    def _stats(self) -> Next24hStats | None:
        return models.next_24h_stats(self.coordinator.points, dt_util.utcnow())

    @property
    def native_value(self) -> float | None:
        stats = self._stats
        return round(stats.avg_mw, 1) if stats is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        stats = self._stats
        if stats is None:
            return {}
        return {
            "min": round(stats.min_mw, 1),
            "min_time": stats.min_time.isoformat(),
            "max": round(stats.max_mw, 1),
            "max_time": stats.max_time.isoformat(),
        }

    @property
    def available(self) -> bool:
        """Available iff there's at least one point in the next-24h window."""
        return self._stats is not None
