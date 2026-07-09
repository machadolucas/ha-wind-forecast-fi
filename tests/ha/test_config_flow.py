"""Config-flow tests: hub singleton + fingrid/fmi/combined subentries."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wind_forecast_fi.const import (
    CONF_API_KEY,
    CONF_GAP_FILL,
    CONF_PRIMARY,
    CONF_SECONDARY,
    DOMAIN,
    SUBENTRY_TYPE_COMBINED,
    SUBENTRY_TYPE_FINGRID,
    SUBENTRY_TYPE_FMI,
)
from custom_components.wind_forecast_fi.fingrid import FINGRID_DATA_URL

_FINGRID_OK_PAYLOAD = {
    "data": [
        {
            "startTime": "2026-07-09T12:00:00.000Z",
            "endTime": "2026-07-09T12:15:00.000Z",
            "value": 900.0,
        }
    ]
}


def _hub_entry(hass: HomeAssistant, **kwargs) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"name": "Wind Forecast Finland"}, unique_id=DOMAIN, **kwargs
    )
    entry.add_to_hass(hass)
    return entry


# ── Hub ───────────────────────────────────────────────────────────────────────


async def test_hub_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Wind Forecast Finland"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["name"] == "Wind Forecast Finland"


async def test_hub_flow_aborts_on_duplicate(hass: HomeAssistant) -> None:
    _hub_entry(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Wind Forecast Finland"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_hub_reconfigure_renames(hass: HomeAssistant) -> None:
    entry = _hub_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Finland Wind"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["name"] == "Finland Wind"


# ── Fingrid subentry ──────────────────────────────────────────────────────────


async def test_fingrid_subentry_happy_path(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _hub_entry(hass)
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_OK_PAYLOAD)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_FINGRID), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_API_KEY: "s3cr3t",
            "update_interval": 15,
            "stale_after_hours": 2,
            "lookback_hours": 12,
            "horizon_hours": 74,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Fingrid"
    assert result["data"][CONF_API_KEY] == "s3cr3t"
    assert result["data"]["horizon_hours"] == 74


async def test_fingrid_subentry_invalid_key_shows_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _hub_entry(hass)
    aioclient_mock.get(FINGRID_DATA_URL, status=401)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_FINGRID), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_API_KEY: "bad-key"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_api_key"


async def test_fingrid_subentry_cannot_connect_shows_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _hub_entry(hass)
    aioclient_mock.get(FINGRID_DATA_URL, exc=TimeoutError())

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_FINGRID), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_API_KEY: "some-key"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_fingrid_subentry_duplicate_aborts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _hub_entry(
        hass,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_FINGRID,
                title="Fingrid",
                unique_id=None,
                data={CONF_API_KEY: "existing"},
            )
        ],
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_FINGRID), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_fingrid_subentry_reconfigure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _hub_entry(
        hass,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_FINGRID,
                title="Fingrid",
                unique_id=None,
                data={CONF_API_KEY: "old-key", "horizon_hours": 74},
            )
        ],
    )
    subentry_id = next(iter(entry.subentries))
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_OK_PAYLOAD)

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_API_KEY: "new-key", "horizon_hours": 48},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data[CONF_API_KEY] == "new-key"
    assert entry.subentries[subentry_id].data["horizon_hours"] == 48


# ── FMI subentry ──────────────────────────────────────────────────────────────


async def test_fmi_subentry_create(hass: HomeAssistant) -> None:
    entry = _hub_entry(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_FMI), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"update_interval": 15, "stale_after_hours": 2}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "FMI"


async def test_fmi_subentry_duplicate_aborts(hass: HomeAssistant) -> None:
    entry = _hub_entry(
        hass,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_FMI, title="FMI", unique_id=None, data={}
            )
        ],
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_FMI), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ── Combined subentry ─────────────────────────────────────────────────────────


async def test_combined_subentry_aborts_without_sources(hass: HomeAssistant) -> None:
    entry = _hub_entry(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMBINED), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_sources"


async def test_combined_subentry_create_with_sources(hass: HomeAssistant) -> None:
    entry = _hub_entry(
        hass,
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
    fingrid_id = next(
        sid for sid, s in entry.subentries.items() if s.subentry_type == SUBENTRY_TYPE_FINGRID
    )
    fmi_id = next(
        sid for sid, s in entry.subentries.items() if s.subentry_type == SUBENTRY_TYPE_FMI
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMBINED), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"].schema
    primary_field = next(k for k in schema if str(k) == CONF_PRIMARY)
    options = {opt["value"] for opt in schema[primary_field].config["options"]}
    assert options == {fingrid_id, fmi_id}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_PRIMARY: fingrid_id, CONF_SECONDARY: fmi_id, CONF_GAP_FILL: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Combined"
    assert result["data"][CONF_PRIMARY] == fingrid_id
    assert result["data"][CONF_SECONDARY] == fmi_id
    assert result["data"][CONF_GAP_FILL] is True


async def test_combined_subentry_duplicate_aborts(hass: HomeAssistant) -> None:
    entry = _hub_entry(
        hass,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_FINGRID,
                title="Fingrid",
                unique_id=None,
                data={CONF_API_KEY: "k"},
            ),
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_COMBINED,
                title="Combined",
                unique_id=None,
                data={CONF_PRIMARY: "whatever", CONF_GAP_FILL: True},
            ),
        ],
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMBINED), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_combined_subentry_reconfigure_prefills(hass: HomeAssistant) -> None:
    entry = _hub_entry(
        hass,
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
    fingrid_id = next(
        sid for sid, s in entry.subentries.items() if s.subentry_type == SUBENTRY_TYPE_FINGRID
    )
    fmi_id = next(
        sid for sid, s in entry.subentries.items() if s.subentry_type == SUBENTRY_TYPE_FMI
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMBINED), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_PRIMARY: fingrid_id, CONF_GAP_FILL: False}
    )
    combined_id = next(
        sid for sid, s in entry.subentries.items() if s.subentry_type == SUBENTRY_TYPE_COMBINED
    )

    result = await entry.start_subentry_reconfigure_flow(hass, combined_id)
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"].schema
    primary_field = next(k for k in schema if str(k) == CONF_PRIMARY)
    assert primary_field.description == {"suggested_value": fingrid_id}
    gap_field = next(k for k in schema if str(k) == CONF_GAP_FILL)
    assert gap_field.default() is False

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_PRIMARY: fingrid_id, CONF_SECONDARY: fmi_id, CONF_GAP_FILL: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[combined_id].data[CONF_SECONDARY] == fmi_id
    assert entry.subentries[combined_id].data[CONF_GAP_FILL] is True
