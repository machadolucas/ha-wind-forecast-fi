"""Binary-sensor platform tests: the per-source/combined ``stale`` health signal."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wind_forecast_fi.const import (
    CONF_API_KEY,
    CONF_STALE_AFTER_HOURS,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    SUBENTRY_TYPE_FINGRID,
)
from custom_components.wind_forecast_fi.fingrid import FINGRID_DATA_URL

_FINGRID_PAYLOAD = {
    "data": [
        {
            "startTime": "2026-07-09T12:00:00.000Z",
            "endTime": "2026-07-09T12:15:00.000Z",
            "value": 900.0,
        }
    ]
}


def _entity_id(hass: HomeAssistant, subentry_id: str) -> str:
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id("binary_sensor", DOMAIN, f"{subentry_id}_stale")
    assert eid is not None, "stale binary_sensor not registered"
    return eid


async def test_stale_off_after_fresh_success(
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
            )
        ],
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_PAYLOAD)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    assert hass.states.get(_entity_id(hass, subentry_id)).state == "off"


async def test_stale_is_on_from_cold_start_with_no_successful_fetch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
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
            )
        ],
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(FINGRID_DATA_URL, status=500)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    state = hass.states.get(_entity_id(hass, subentry_id))
    # Always available (it *reports* health) even with zero cached data.
    assert state.state == "on"
    assert state.state != "unavailable"


async def test_stale_flips_on_after_prolonged_failure_via_coordinator_polls(
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
                data={
                    CONF_API_KEY: "k",
                    CONF_UPDATE_INTERVAL: 15,
                    CONF_STALE_AFTER_HOURS: 2,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_PAYLOAD)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    entity_id = _entity_id(hass, subentry_id)
    assert hass.states.get(entity_id).state == "off"

    # Every subsequent poll fails, but cached points keep the coordinator
    # "successful" (never-go-empty) — it still ticks and notifies listeners
    # every 15 minutes (see binary_sensor.py's module docstring).
    aioclient_mock.clear_requests()
    aioclient_mock.get(FINGRID_DATA_URL, status=500)

    # 2h stale_after_hours / 15-min polls = 8 ticks to cross the threshold;
    # go a little past to be safely on the "stale" side.
    for _ in range(10):
        freezer.tick(timedelta(minutes=15))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "on"
