"""Coordinators: per-source (Fingrid/FMI) polling + a combined merge coordinator.

Implements the plan's never-go-empty contract:

- ``async_seed_from_store`` loads the persisted snapshot *before* the first
  refresh, so entities are populated even if the network is down at boot.
- On a fetch failure, cached points (if any) are served unchanged — the
  coordinator stays "successful" — and ``UpdateFailed`` is only raised when
  there is truly nothing to serve.
- ``stale``/``last_successful_fetch`` are tracked independently of HA's
  ``last_update_success``, which is what drives the stale binary_sensor
  (added in a later phase) without flapping entity availability on every
  transient failure.

``CombinedCoordinator`` does no fetching of its own: it listens to its source
coordinator(s) and recomputes a merged view with ``models.merge_forecasts``.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import models
from .const import (
    CONF_API_KEY,
    CONF_HORIZON_HOURS,
    CONF_LOOKBACK_HOURS,
    CONF_STALE_AFTER_HOURS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_LOOKBACK_HOURS,
    DEFAULT_STALE_AFTER_HOURS,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    ISSUE_SOURCE_STALE,
    RETENTION_PAST_HOURS,
    STALE_REPAIR_FACTOR,
)
from .fingrid import FingridApiError, async_fetch_fingrid
from .fmi import FmiApiError, async_fetch_fmi
from .models import ForecastPoint
from .persistence import ForecastStore

_LOGGER = logging.getLogger(__name__)


class SourceCoordinator(DataUpdateCoordinator[list[ForecastPoint]]):
    """Base for a single-source (Fingrid/FMI) polling coordinator."""

    source_kind: str
    # Exceptions a subclass's ``_async_fetch`` may raise on a source failure;
    # anything else is a programming error and propagates normally.
    _fetch_errors: tuple[type[Exception], ...] = (TimeoutError,)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        data = subentry.data
        interval = data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{subentry.subentry_id}",
            update_interval=timedelta(minutes=interval),
        )
        self.subentry = subentry
        self.stale_after_hours: float = data.get(CONF_STALE_AFTER_HOURS, DEFAULT_STALE_AFTER_HOURS)
        self._points: list[ForecastPoint] = []
        self.last_successful_fetch: datetime | None = None
        self._failures = 0
        self._store = ForecastStore(hass, subentry.subentry_id)

    @property
    def points(self) -> list[ForecastPoint]:
        """Current cached points — safe to read even before the first refresh."""
        return self._points

    @property
    def stale(self) -> bool:
        """True when there's no successful fetch yet, or it's past the threshold."""
        if self.last_successful_fetch is None:
            return True
        age = dt_util.utcnow() - self.last_successful_fetch
        return age > timedelta(hours=self.stale_after_hours)

    async def async_seed_from_store(self) -> None:
        """Load the persisted snapshot before the first refresh (best-effort).

        Populates ``points``/``last_successful_fetch`` from the last successful
        run so entities have data even if the very first fetch fails. A missing
        or empty Store file is a normal cold start, not an error.
        """
        snapshot = await self._store.async_load()
        if not snapshot:
            return
        self._points = models.points_from_storage(snapshot.get("points", []))
        raw_fetch = snapshot.get("last_successful_fetch")
        self.last_successful_fetch = dt_util.parse_datetime(raw_fetch) if raw_fetch else None

    @abstractmethod
    async def _async_fetch(self) -> list[ForecastPoint]:
        """Fetch fresh points from the source. Raise one of ``_fetch_errors``."""

    @callback
    def _update_repair_issue(self) -> None:
        """Raise/clear the per-source stale repair issue.

        Mirrors ``LoadSchedulerCoordinator._update_price_issue``: raised once a
        source has been failing for at least ``stale_after_hours *
        STALE_REPAIR_FACTOR`` with no successful fetch, cleared as soon as a
        fetch succeeds (or before it's been failing long enough to matter).
        """
        issue_id = f"{ISSUE_SOURCE_STALE}_{self.subentry.subentry_id}"
        threshold = timedelta(hours=self.stale_after_hours * STALE_REPAIR_FACTOR)
        long_stale = self.last_successful_fetch is None or (
            dt_util.utcnow() - self.last_successful_fetch > threshold
        )
        if long_stale and self._failures > 0:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_SOURCE_STALE,
                translation_placeholders={"source": self.subentry.title},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def _async_update_data(self) -> list[ForecastPoint]:
        try:
            fetched = await self._async_fetch()
        except self._fetch_errors as err:
            self._failures += 1
            self._update_repair_issue()
            if self._points:
                _LOGGER.warning(
                    "%s fetch failed (%s); serving %d cached point(s)",
                    self.source_kind,
                    err,
                    len(self._points),
                )
                return self._points
            raise UpdateFailed(f"{self.source_kind} fetch failed and no cache: {err}") from err

        self._failures = 0
        self.last_successful_fetch = dt_util.utcnow()
        self._points = models.trim_past(
            fetched, self.last_successful_fetch, timedelta(hours=RETENTION_PAST_HOURS)
        )
        self._update_repair_issue()
        self._store.async_schedule_save(
            {
                "points": models.points_to_storage(self._points),
                "last_successful_fetch": self.last_successful_fetch.isoformat(),
            }
        )
        return self._points


class FingridCoordinator(SourceCoordinator):
    """Polls Fingrid dataset 245 over ``[now - lookback, now + horizon]``."""

    source_kind = "fingrid"
    _fetch_errors = (FingridApiError, TimeoutError)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        super().__init__(hass, entry, subentry)
        self._api_key: str = subentry.data[CONF_API_KEY]
        self._lookback_hours: float = subentry.data.get(CONF_LOOKBACK_HOURS, DEFAULT_LOOKBACK_HOURS)
        self._horizon_hours: float = subentry.data.get(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS)

    async def _async_fetch(self) -> list[ForecastPoint]:
        session = async_get_clientsession(self.hass)
        now = dt_util.utcnow()
        start = now - timedelta(hours=self._lookback_hours)
        end = now + timedelta(hours=self._horizon_hours)
        return await async_fetch_fingrid(session, self._api_key, start=start, end=end)


class FmiCoordinator(SourceCoordinator):
    """Polls the public FMI wind-forecast CDN endpoint."""

    source_kind = "fmi"
    _fetch_errors = (FmiApiError, TimeoutError)

    async def _async_fetch(self) -> list[ForecastPoint]:
        session = async_get_clientsession(self.hass)
        return await async_fetch_fmi(session)


class CombinedCoordinator(DataUpdateCoordinator[list[ForecastPoint]]):
    """Recomputes a merged forecast whenever a source coordinator updates.

    Does no fetching itself (``update_interval=None``); ``start()`` wires up
    listeners on the configured source(s) and does an initial recompute.
    """

    source_kind = "combined"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
        primary: SourceCoordinator,
        secondary: SourceCoordinator | None,
        gap_fill: bool,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{subentry.subentry_id}",
            update_interval=None,
        )
        self.subentry = subentry
        self.primary = primary
        self.secondary = secondary
        self.gap_fill = gap_fill
        self._unsubs: list[Callable[[], None]] = []

    @property
    def points(self) -> list[ForecastPoint]:
        """Current merged points — safe to read even before the first recompute."""
        return self.data or []

    @property
    def last_successful_fetch(self) -> datetime | None:
        """Most recent successful fetch across contributing sources, if any."""
        candidates = [self.primary.last_successful_fetch]
        if self.secondary is not None:
            candidates.append(self.secondary.last_successful_fetch)
        known = [c for c in candidates if c is not None]
        return max(known) if known else None

    @property
    def stale(self) -> bool:
        """Stale only once every contributing source is stale."""
        if self.secondary is None:
            return self.primary.stale
        return self.primary.stale and self.secondary.stale

    @callback
    def async_recompute(self) -> None:
        """Rebuild the merged view from the current source points."""
        merged = models.merge_forecasts(
            self.primary.points,
            self.secondary.points if self.secondary is not None else [],
            self.gap_fill,
        )
        self.async_set_updated_data(merged)

    @callback
    def start(self) -> list[Callable[[], None]]:
        """Subscribe to source updates and do an initial recompute.

        Returns the unsubscribe callbacks (also stored on ``self`` so
        ``async_shutdown`` cleans them up); the caller (phase-4 ``__init__.py``)
        may additionally wire them into ``entry.async_on_unload``.
        """
        self._unsubs = [self.primary.async_add_listener(self.async_recompute)]
        if self.secondary is not None:
            self._unsubs.append(self.secondary.async_add_listener(self.async_recompute))
        self.async_recompute()
        return list(self._unsubs)

    async def async_shutdown(self) -> None:
        """Unsubscribe from sources, then defer to the base shutdown."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        await super().async_shutdown()
