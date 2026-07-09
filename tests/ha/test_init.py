"""Minimal ``__init__.py`` wiring: coordinators built, never-go-empty on setup.

No entity platforms yet (later phase) — this only exercises
``async_setup_entry``/``async_unload_entry`` building/tearing down the
coordinators dict on ``entry.runtime_data``.
"""

from __future__ import annotations

from types import MappingProxyType

from homeassistant.config_entries import ConfigSubentry, ConfigSubentryData
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wind_forecast_fi.const import (
    CONF_API_KEY,
    CONF_PRIMARY,
    DOMAIN,
    SUBENTRY_TYPE_COMBINED,
    SUBENTRY_TYPE_FINGRID,
    SUBENTRY_TYPE_FMI,
)
from custom_components.wind_forecast_fi.fingrid import FINGRID_DATA_URL
from custom_components.wind_forecast_fi.fmi import FMI_FORECAST_URL

_FINGRID_PAYLOAD = {
    "data": [
        {
            "startTime": "2026-07-09T12:00:00.000Z",
            "endTime": "2026-07-09T12:15:00.000Z",
            "value": 900.0,
        }
    ]
}
_FMI_PAYLOAD = {"series": [{"data": [[1783594800000.0, 1.0]]}]}


def _hub_with_all(hass: HomeAssistant) -> MockConfigEntry:
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
    combined_subentry = ConfigSubentry(
        data=MappingProxyType({CONF_PRIMARY: fingrid_id}),
        subentry_type=SUBENTRY_TYPE_COMBINED,
        title="Combined",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(entry, combined_subentry)
    return entry


async def test_setup_and_unload_build_and_teardown_coordinators(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _hub_with_all(hass)
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_PAYLOAD)
    aioclient_mock.get(FMI_FORECAST_URL, json=_FMI_PAYLOAD)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    assert len(runtime.sources) == 2
    assert len(runtime.combined) == 1
    combined = next(iter(runtime.combined.values()))
    assert combined.points  # merged from both healthy sources

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_survives_dead_source_with_no_cache(
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

    # A dead source with nothing cached must not block hub setup.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = next(iter(entry.runtime_data.sources.values()))
    assert coordinator.last_update_success is False
    assert coordinator.points == []
