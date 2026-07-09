"""Config flow for Wind Forecast Finland.

Topology (mirrors ha-load-scheduler / ha-load-need-predictor): a singleton hub
entry (``unique_id = DOMAIN`` — only one Finland) plus one ``ConfigSubentry``
per source (``fingrid``, ``fmi``) and an optional ``combined`` subentry that
merges them. Each subentry type is a singleton within the hub and shares one
``init`` step between add and reconfigure.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    CONF_API_KEY,
    CONF_GAP_FILL,
    CONF_HORIZON_HOURS,
    CONF_LOOKBACK_HOURS,
    CONF_NAME,
    CONF_PRIMARY,
    CONF_SECONDARY,
    CONF_STALE_AFTER_HOURS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_GAP_FILL,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_LOOKBACK_HOURS,
    DEFAULT_NAME,
    DEFAULT_STALE_AFTER_HOURS,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    SUBENTRY_TYPE_COMBINED,
    SUBENTRY_TYPE_FINGRID,
    SUBENTRY_TYPE_FMI,
    UPDATE_INTERVAL_MINUTES_FLOOR,
)
from .fingrid import FingridApiError, FingridAuthError, async_fetch_fingrid

_FINGRID_TITLE = "Fingrid"
_FMI_TITLE = "FMI"
_COMBINED_TITLE = "Combined"

# A small window is enough to prove the key works without pulling real data.
_PROBE_WINDOW = timedelta(minutes=15)
_PROBE_PAGE_SIZE = 10


def _clean(user_input: dict) -> dict:
    """Drop unset optionals so model defaults apply cleanly."""
    return {k: v for k, v in user_input.items() if v not in (None, "")}


def _existing_subentries_of_type(
    entry: ConfigEntry, subentry_type: str
) -> dict[str, ConfigSubentry]:
    return {sid: sub for sid, sub in entry.subentries.items() if sub.subentry_type == subentry_type}


def _source_options(entry: ConfigEntry) -> list[selector.SelectOptionDict]:
    """Existing fingrid/fmi subentries of the hub, as combined-picker options."""
    return [
        selector.SelectOptionDict(value=sid, label=sub.title)
        for sid, sub in entry.subentries.items()
        if sub.subentry_type in (SUBENTRY_TYPE_FINGRID, SUBENTRY_TYPE_FMI)
    ]


def _minutes_selector(minimum: int, maximum: int) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            unit_of_measurement="min",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _hours_selector(minimum: float, maximum: float) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            unit_of_measurement="h",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _hub_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)
            ): selector.TextSelector(),
        }
    )


def _fingrid_schema(defaults: dict) -> vol.Schema:
    def suggest(key: str) -> dict:
        return {"suggested_value": defaults.get(key)}

    return vol.Schema(
        {
            vol.Required(CONF_API_KEY, description=suggest(CONF_API_KEY)): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(
                CONF_UPDATE_INTERVAL,
                default=defaults.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES),
            ): _minutes_selector(UPDATE_INTERVAL_MINUTES_FLOOR, 120),
            vol.Optional(
                CONF_STALE_AFTER_HOURS,
                default=defaults.get(CONF_STALE_AFTER_HOURS, DEFAULT_STALE_AFTER_HOURS),
            ): _hours_selector(1, 24),
            vol.Optional(
                CONF_LOOKBACK_HOURS,
                default=defaults.get(CONF_LOOKBACK_HOURS, DEFAULT_LOOKBACK_HOURS),
            ): _hours_selector(0, 48),
            vol.Optional(
                CONF_HORIZON_HOURS,
                default=defaults.get(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS),
            ): _hours_selector(24, 120),
        }
    )


def _fmi_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_UPDATE_INTERVAL,
                default=defaults.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES),
            ): _minutes_selector(UPDATE_INTERVAL_MINUTES_FLOOR, 120),
            vol.Optional(
                CONF_STALE_AFTER_HOURS,
                default=defaults.get(CONF_STALE_AFTER_HOURS, DEFAULT_STALE_AFTER_HOURS),
            ): _hours_selector(1, 24),
        }
    )


def _combined_schema(defaults: dict, options: list[selector.SelectOptionDict]) -> vol.Schema:
    def suggest(key: str) -> dict:
        return {"suggested_value": defaults.get(key)}

    picker = selector.SelectSelector(
        selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
    )
    return vol.Schema(
        {
            vol.Required(CONF_PRIMARY, description=suggest(CONF_PRIMARY)): picker,
            vol.Optional(CONF_SECONDARY, description=suggest(CONF_SECONDARY)): picker,
            vol.Optional(
                CONF_GAP_FILL, default=defaults.get(CONF_GAP_FILL, DEFAULT_GAP_FILL)
            ): selector.BooleanSelector(),
        }
    )


class WindForecastFiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the (singleton) hub config flow."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create the hub. Only one Finland — sources are added as subentries."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, DEFAULT_NAME), data=user_input
            )
        return self.async_show_form(step_id="user", data_schema=_hub_schema({}))

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Rename the hub."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data_updates=user_input)
        defaults = {**entry.data, **(user_input or {})}
        return self.async_show_form(step_id="reconfigure", data_schema=_hub_schema(defaults))

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Sources (fingrid/fmi) and the combined view are added as subentries."""
        return {
            SUBENTRY_TYPE_FINGRID: FingridSubentryFlowHandler,
            SUBENTRY_TYPE_FMI: FmiSubentryFlowHandler,
            SUBENTRY_TYPE_COMBINED: CombinedSubentryFlowHandler,
        }


class FingridSubentryFlowHandler(ConfigSubentryFlow):
    """Add or reconfigure the (singleton) Fingrid source subentry.

    Adding validates the API key with a small live test-fetch: 401/403 maps to
    ``invalid_api_key``, any other failure (network, timeout, bad response) to
    ``cannot_connect``.
    """

    _defaults: dict

    @property
    def _is_new(self) -> bool:
        return self.source == SOURCE_USER

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if _existing_subentries_of_type(self._get_entry(), SUBENTRY_TYPE_FINGRID):
            return self.async_abort(reason="already_configured")
        self._defaults = {}
        return await self.async_step_init(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        self._defaults = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_init(user_input)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean(user_input)
            session = async_get_clientsession(self.hass)
            now = dt_util.utcnow()
            try:
                await async_fetch_fingrid(
                    session,
                    data[CONF_API_KEY],
                    start=now - _PROBE_WINDOW,
                    end=now + _PROBE_WINDOW,
                    page_size=_PROBE_PAGE_SIZE,
                )
            except FingridAuthError:
                errors["base"] = "invalid_api_key"
            except FingridApiError:
                errors["base"] = "cannot_connect"
            if not errors:
                if self._is_new:
                    return self.async_create_entry(title=_FINGRID_TITLE, data=data)
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    title=_FINGRID_TITLE,
                    data=data,
                )
        return self.async_show_form(
            step_id="init", data_schema=_fingrid_schema(self._defaults), errors=errors
        )


class FmiSubentryFlowHandler(ConfigSubentryFlow):
    """Add or reconfigure the (singleton) FMI source subentry.

    Public endpoint, no key — no network check on submit; the coordinator's
    stale machinery reports problems after setup instead.
    """

    _defaults: dict

    @property
    def _is_new(self) -> bool:
        return self.source == SOURCE_USER

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if _existing_subentries_of_type(self._get_entry(), SUBENTRY_TYPE_FMI):
            return self.async_abort(reason="already_configured")
        self._defaults = {}
        return await self.async_step_init(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        self._defaults = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_init(user_input)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            data = _clean(user_input)
            if self._is_new:
                return self.async_create_entry(title=_FMI_TITLE, data=data)
            return self.async_update_and_abort(
                self._get_entry(), self._get_reconfigure_subentry(), title=_FMI_TITLE, data=data
            )
        return self.async_show_form(step_id="init", data_schema=_fmi_schema(self._defaults))


class CombinedSubentryFlowHandler(ConfigSubentryFlow):
    """Add or reconfigure the (singleton) combined subentry.

    Primary/secondary are picked by subentry id from the hub's existing
    fingrid/fmi subentries; aborts with ``no_sources`` if none exist yet.
    """

    _defaults: dict

    @property
    def _is_new(self) -> bool:
        return self.source == SOURCE_USER

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        entry = self._get_entry()
        if _existing_subentries_of_type(entry, SUBENTRY_TYPE_COMBINED):
            return self.async_abort(reason="already_configured")
        if not _source_options(entry):
            return self.async_abort(reason="no_sources")
        self._defaults = {}
        return await self.async_step_init(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        self._defaults = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_init(user_input)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        options = _source_options(self._get_entry())
        if user_input is not None:
            data = _clean(user_input)
            if self._is_new:
                return self.async_create_entry(title=_COMBINED_TITLE, data=data)
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=_COMBINED_TITLE,
                data=data,
            )
        return self.async_show_form(
            step_id="init", data_schema=_combined_schema(self._defaults, options)
        )
