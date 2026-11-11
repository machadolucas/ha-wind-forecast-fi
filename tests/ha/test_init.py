"""``__init__.py`` wiring: coordinators + platforms built, torn down cleanly.

Exercises ``async_setup_entry``/``async_unload_entry`` building/tearing down
the coordinators dict on ``entry.runtime_data`` *and* the forwarded
``sensor``/``binary_sensor`` platforms, plus the reload-on-subentry-change
listener.
"""

from __future__ import annotations

from types import MappingProxyType

from homeassistant.config_entries import ConfigEntryState, ConfigSubentry, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    # Keep the dated forecast payload inside the coordinator's retention window.
    freezer.move_to("2026-07-09T12:00:00+00:00")
    entry = _hub_with_all(hass)
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_PAYLOAD)
    aioclient_mock.get(FMI_FORECAST_URL, json=_FMI_PAYLOAD)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    assert len(runtime.sources) == 2
    assert len(runtime.combined) == 1
    assert all(source.points for source in runtime.sources.values())
    combined = next(iter(runtime.combined.values()))
    assert combined.points  # merged from both healthy sources

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unload_removes_entities_shuts_down_coordinators_and_listeners(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Unload must be a clean teardown: entities, coordinator listeners/timers,

    and the reload update-listener registered in ``async_setup_entry`` should
    all be gone afterwards (no lingering state to leak into a future setup).
    """
    entry = _hub_with_all(hass)
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_PAYLOAD)
    aioclient_mock.get(FMI_FORECAST_URL, json=_FMI_PAYLOAD)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    combined = next(iter(runtime.combined.values()))
    assert combined._unsubs  # subscribed to its sources while loaded

    reg = er.async_get(hass)
    entity_ids = [
        eid for eid in reg.entities if reg.entities[eid].config_entry_id == entry.entry_id
    ]
    assert entity_ids  # sensor/binary_sensor entities were registered
    assert any(hass.states.get(eid) is not None for eid in entity_ids)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert combined._unsubs == []  # coordinator listeners cleaned up
    assert not entry.update_listeners  # the reload listener unregistered itself
    # HA leaves a restorable "unavailable" placeholder rather than removing the
    # state outright; either way, no entity is still reporting live data.
    assert all(hass.states.get(eid).state == "unavailable" for eid in entity_ids)


async def test_adding_a_subentry_reloads_the_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Subentry mutation fires the entry's update listener, which reloads."""
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
    aioclient_mock.get(FMI_FORECAST_URL, json=_FMI_PAYLOAD)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(entry.runtime_data.sources) == 1

    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType({}), subentry_type=SUBENTRY_TYPE_FMI, title="FMI", unique_id=None
        ),
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert len(entry.runtime_data.sources) == 2  # reload picked up the new FMI source


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
