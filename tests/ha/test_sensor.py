"""Sensor platform tests: main forecast sensor + derived next-24h sensor.

Full-integration style (mirrors ``tests/ha/test_init.py``/``test_coordinator.py``):
a real ``MockConfigEntry`` + ``aioclient_mock`` drive ``async_setup_entry``,
then assertions read straight off ``hass.states``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

from homeassistant.config_entries import ConfigSubentry, ConfigSubentryData
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wind_forecast_fi.const import (
    CONF_API_KEY,
    CONF_PRIMARY,
    CONF_SECONDARY,
    DOMAIN,
    STORAGE_VERSION,
    SUBENTRY_TYPE_COMBINED,
    SUBENTRY_TYPE_FINGRID,
    SUBENTRY_TYPE_FMI,
)
from custom_components.wind_forecast_fi.fingrid import FINGRID_DATA_URL
from custom_components.wind_forecast_fi.fmi import FMI_FORECAST_URL
from custom_components.wind_forecast_fi.models import ForecastPoint, points_to_storage
from custom_components.wind_forecast_fi.sensor import WindForecastSensor

_FINGRID_TWO_BUCKETS = {
    "data": [
        {
            "startTime": "2026-07-09T12:00:00.000Z",
            "endTime": "2026-07-09T12:15:00.000Z",
            "value": 900.0,
        },
        {
            "startTime": "2026-07-09T12:15:00.000Z",
            "endTime": "2026-07-09T12:30:00.000Z",
            "value": 950.0,
        },
    ]
}
_FINGRID_TWO_HOURS = {
    "data": [
        {
            "startTime": "2026-07-09T12:00:00.000Z",
            "endTime": "2026-07-09T13:00:00.000Z",
            "value": 100.0,
        },
        {
            "startTime": "2026-07-09T13:00:00.000Z",
            "endTime": "2026-07-09T14:00:00.000Z",
            "value": 300.0,
        },
    ]
}


def _entity_id(hass: HomeAssistant, platform: str, subentry_id: str, key: str) -> str:
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id(platform, DOMAIN, f"{subentry_id}_{key}")
    assert eid is not None, f"{platform}.{key} not registered for subentry {subentry_id}"
    return eid


def _fingrid_entry(hass: HomeAssistant, **extra_data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub"},
        unique_id=DOMAIN,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_FINGRID,
                title="Fingrid",
                unique_id=None,
                data={CONF_API_KEY: "k", **extra_data},
            )
        ],
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(entry: MockConfigEntry, hass: HomeAssistant) -> str:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return next(iter(entry.subentries))


# ── Main forecast sensor ─────────────────────────────────────────────────────


async def test_main_sensor_state_and_canonical_attributes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    freezer.move_to("2026-07-09T12:05:00+00:00")
    entry = _fingrid_entry(hass)
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_TWO_BUCKETS)
    subentry_id = await _setup(entry, hass)

    state = hass.states.get(_entity_id(hass, "sensor", subentry_id, "forecast"))
    assert state is not None
    assert float(state.state) == 900.0  # inside the 12:00-12:15 bucket

    start0 = datetime(2026, 7, 9, 12, 0, tzinfo=UTC).isoformat()
    end0 = datetime(2026, 7, 9, 12, 15, tzinfo=UTC).isoformat()
    end1 = datetime(2026, 7, 9, 12, 30, tzinfo=UTC).isoformat()
    assert state.attributes["forecast"] == [
        {"start": start0, "end": end0, "value": 900.0},
        {"start": end0, "end": end1, "value": 950.0},
    ]
    assert state.attributes["unit"] == "MW"
    assert state.attributes["source"] == "fingrid"
    assert state.attributes["stale"] is False
    assert state.attributes["last_successful_fetch"] is not None
    assert state.attributes["horizon_start"] == start0
    assert state.attributes["horizon_end"] == end1


def test_forecast_attribute_is_unrecorded() -> None:
    """The bulky per-bucket list must never hit the recorder DB."""
    assert WindForecastSensor._unrecorded_attributes == frozenset({"forecast"})


async def test_main_sensor_unavailable_with_no_cache_and_failing_fetch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _fingrid_entry(hass)
    aioclient_mock.get(FINGRID_DATA_URL, status=500)
    subentry_id = await _setup(entry, hass)

    state = hass.states.get(_entity_id(hass, "sensor", subentry_id, "forecast"))
    assert state.state == STATE_UNAVAILABLE


async def test_main_sensor_serves_seeded_store_cache_when_fetch_fails(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    freezer.move_to("2026-07-09T12:05:00+00:00")
    entry = _fingrid_entry(hass)
    subentry_id = next(iter(entry.subentries))

    cached_point = ForecastPoint(
        start=datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
        end=datetime(2026, 7, 9, 13, 0, tzinfo=UTC),
        value_mw=555.0,
    )
    store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{subentry_id}")
    await store.async_save(
        {
            "points": points_to_storage([cached_point]),
            "last_successful_fetch": datetime(2026, 7, 9, 11, 0, tzinfo=UTC).isoformat(),
        }
    )

    aioclient_mock.get(FINGRID_DATA_URL, status=500)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id(hass, "sensor", subentry_id, "forecast"))
    assert state.state != STATE_UNAVAILABLE
    assert float(state.state) == 555.0
    # last_successful_fetch (11:00) is only 1h5m before "now" (12:05), under
    # the default 2h stale_after_hours threshold.
    assert state.attributes["stale"] is False


# ── Next-24h derived sensor ──────────────────────────────────────────────────


async def test_next_24h_sensor_avg_min_max_times(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    freezer.move_to("2026-07-09T12:00:00+00:00")
    entry = _fingrid_entry(hass)
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_TWO_HOURS)
    subentry_id = await _setup(entry, hass)

    state = hass.states.get(_entity_id(hass, "sensor", subentry_id, "next_24h"))
    assert state is not None
    assert float(state.state) == 200.0  # avg(100, 300)
    assert state.attributes["min"] == 100.0
    assert state.attributes["min_time"] == datetime(2026, 7, 9, 12, 0, tzinfo=UTC).isoformat()
    assert state.attributes["max"] == 300.0
    assert state.attributes["max_time"] == datetime(2026, 7, 9, 13, 0, tzinfo=UTC).isoformat()


async def test_next_24h_sensor_unavailable_without_points(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _fingrid_entry(hass)
    aioclient_mock.get(FINGRID_DATA_URL, status=500)
    subentry_id = await _setup(entry, hass)

    state = hass.states.get(_entity_id(hass, "sensor", subentry_id, "next_24h"))
    assert state.state == STATE_UNAVAILABLE


# ── Combined device/entities ─────────────────────────────────────────────────


async def test_combined_device_and_sensors_merge_both_sources(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    freezer.move_to("2026-07-09T10:00:00+00:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub"},
        unique_id=DOMAIN,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_FINGRID,
                title="Fingrid",
                unique_id=None,
                data={CONF_API_KEY: "k"},
            ),
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_FMI, title="FMI", unique_id=None, data={}
            ),
        ],
    )
    entry.add_to_hass(hass)
    fingrid_id = next(
        sid for sid, s in entry.subentries.items() if s.subentry_type == SUBENTRY_TYPE_FINGRID
    )
    fmi_id = next(
        sid for sid, s in entry.subentries.items() if s.subentry_type == SUBENTRY_TYPE_FMI
    )
    combined_subentry = ConfigSubentry(
        data=MappingProxyType({CONF_PRIMARY: fingrid_id, CONF_SECONDARY: fmi_id}),
        subentry_type=SUBENTRY_TYPE_COMBINED,
        title="Combined",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(entry, combined_subentry)

    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_TWO_BUCKETS)
    # 13:00 UTC — strictly after the fingrid horizon (12:00-12:30), so this
    # hour is a true gap the combined view should fill from FMI.
    aioclient_mock.get(FMI_FORECAST_URL, json={"series": [{"data": [[1783602000000.0, 1.0]]}]})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    combined_id = next(
        sid for sid, s in entry.subentries.items() if s.subentry_type == SUBENTRY_TYPE_COMBINED
    )

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, combined_id), entry.entry_id
    )
    assert device is not None
    assert device.name == "Combined"

    state = hass.states.get(_entity_id(hass, "sensor", combined_id, "forecast"))
    assert state is not None
    assert state.attributes["source"] == "combined"
    # Merged: 2 fingrid (primary) buckets + the 1 FMI-only hour (gap-fill).
    assert len(state.attributes["forecast"]) == 3
