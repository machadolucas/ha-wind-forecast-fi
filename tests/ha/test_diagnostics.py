"""Diagnostics tests: api_key redaction + per-source health block."""

from __future__ import annotations

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wind_forecast_fi.const import CONF_API_KEY, DOMAIN, SUBENTRY_TYPE_FINGRID
from custom_components.wind_forecast_fi.diagnostics import async_get_config_entry_diagnostics
from custom_components.wind_forecast_fi.fingrid import FINGRID_DATA_URL

_FINGRID_PAYLOAD = {
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


async def test_diagnostics_redacts_api_key_and_reports_health(
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
                data={CONF_API_KEY: "super-secret-key"},
            )
        ],
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_PAYLOAD)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["hub"] == {"name": "Hub"}
    source = diagnostics["sources"][subentry_id]
    assert source["type"] == SUBENTRY_TYPE_FINGRID
    assert source["title"] == "Fingrid"
    assert source["config"][CONF_API_KEY] == REDACTED
    assert "super-secret-key" not in str(diagnostics)

    health = source["health"]
    assert health["stale"] is False
    assert health["failures"] == 0
    assert health["n_points"] == 2
    assert health["last_successful_fetch"] is not None
    assert health["horizon_start"] is not None
    assert health["horizon_end"] is not None


async def test_diagnostics_health_reflects_failing_source(
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
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    health = diagnostics["sources"][subentry_id]["health"]
    assert health["stale"] is True
    assert health["failures"] == 1
    assert health["n_points"] == 0
    assert health["last_successful_fetch"] is None
    assert health["horizon_start"] is None
    assert health["horizon_end"] is None
