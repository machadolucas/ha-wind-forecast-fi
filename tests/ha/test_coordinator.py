"""Coordinator tests: never-go-empty contract, Store seeding, stale, repairs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wind_forecast_fi.const import (
    CONF_API_KEY,
    CONF_GAP_FILL,
    CONF_PRIMARY,
    CONF_SECONDARY,
    DOMAIN,
    ISSUE_SOURCE_STALE,
    STORAGE_VERSION,
    SUBENTRY_TYPE_COMBINED,
    SUBENTRY_TYPE_FINGRID,
    SUBENTRY_TYPE_FMI,
)
from custom_components.wind_forecast_fi.coordinator import (
    CombinedCoordinator,
    FingridCoordinator,
    FmiCoordinator,
)
from custom_components.wind_forecast_fi.fingrid import FINGRID_DATA_URL
from custom_components.wind_forecast_fi.fmi import FMI_FORECAST_URL
from custom_components.wind_forecast_fi.models import ForecastPoint, points_to_storage

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


def _point(hour: int, value: float = 500.0) -> ForecastPoint:
    start = datetime(2026, 7, 9, hour, tzinfo=UTC)
    return ForecastPoint(start=start, end=start + timedelta(hours=1), value_mw=value)


def _hub_with_fingrid(hass: HomeAssistant, *, api_key: str = "secret-key") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub"},
        unique_id=DOMAIN,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_FINGRID,
                title="Fingrid",
                unique_id=None,
                data={"api_key": api_key},
            )
        ],
    )
    entry.add_to_hass(hass)
    return entry


def _hub_with_fmi(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub"},
        unique_id=DOMAIN,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_FMI, title="FMI", unique_id=None, data={}
            )
        ],
    )
    entry.add_to_hass(hass)
    return entry


def _fingrid_subentry(entry: MockConfigEntry):
    return next(iter(entry.subentries.values()))


# ── Fingrid coordinator: fetch success/failure, seeding, stale ──────────────


async def test_successful_fetch_parses_trims_and_saves_snapshot(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    freezer.move_to("2026-07-09T13:00:00+00:00")
    entry = _hub_with_fingrid(hass)
    subentry = _fingrid_subentry(entry)
    coordinator = FingridCoordinator(hass, entry, subentry)
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_PAYLOAD)

    saved_snapshots: list[dict] = []
    coordinator._store.async_schedule_save = saved_snapshots.append  # type: ignore[method-assign]

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert [p.value_mw for p in coordinator.points] == [900.0, 950.0]
    assert coordinator.last_successful_fetch == dt_util.utcnow()

    # A save was scheduled (debounced) with the trimmed points + fetch time.
    assert len(saved_snapshots) == 1
    assert len(saved_snapshots[0]["points"]) == 2
    assert (
        saved_snapshots[0]["last_successful_fetch"] == coordinator.last_successful_fetch.isoformat()
    )

    # Flushing the real Store (bypassing the debounce) round-trips correctly.
    await coordinator._store.async_save_now(saved_snapshots[0])
    store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{subentry.subentry_id}")
    saved = await store.async_load()
    assert saved is not None
    assert len(saved["points"]) == 2
    assert saved["last_successful_fetch"] is not None


async def test_fetch_failure_with_prior_points_serves_cache(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _hub_with_fingrid(hass)
    subentry = _fingrid_subentry(entry)
    coordinator = FingridCoordinator(hass, entry, subentry)
    coordinator._points = [_point(10), _point(11)]
    aioclient_mock.get(FINGRID_DATA_URL, status=500)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data == [_point(10), _point(11)]


async def test_fetch_failure_with_no_points_raises_update_failed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _hub_with_fingrid(hass)
    subentry = _fingrid_subentry(entry)
    coordinator = FingridCoordinator(hass, entry, subentry)
    aioclient_mock.get(FINGRID_DATA_URL, status=500)

    try:
        await coordinator._async_update_data()
    except UpdateFailed:
        pass
    else:
        raise AssertionError("expected UpdateFailed")

    # Through the coordinator machinery, the same failure is swallowed (never
    # raised to the caller) but correctly marks the refresh unsuccessful.
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False


async def test_seed_from_store_restores_points_and_last_fetch(hass: HomeAssistant) -> None:
    entry = _hub_with_fingrid(hass)
    subentry = _fingrid_subentry(entry)
    store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{subentry.subentry_id}")
    fetch_time = datetime(2026, 7, 9, 9, tzinfo=UTC)
    await store.async_save(
        {
            "points": points_to_storage([_point(1), _point(2)]),
            "last_successful_fetch": fetch_time.isoformat(),
        }
    )

    coordinator = FingridCoordinator(hass, entry, subentry)
    await coordinator.async_seed_from_store()

    assert coordinator.points == [_point(1), _point(2)]
    assert coordinator.last_successful_fetch == fetch_time


async def test_seed_from_store_noop_when_nothing_saved(hass: HomeAssistant) -> None:
    entry = _hub_with_fingrid(hass)
    subentry = _fingrid_subentry(entry)
    coordinator = FingridCoordinator(hass, entry, subentry)

    await coordinator.async_seed_from_store()

    assert coordinator.points == []
    assert coordinator.last_successful_fetch is None


async def test_stale_property_thresholds(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-07-09T12:00:00+00:00")
    entry = _hub_with_fingrid(hass)
    subentry = _fingrid_subentry(entry)
    coordinator = FingridCoordinator(hass, entry, subentry)

    assert coordinator.stale is True  # never fetched

    coordinator.last_successful_fetch = dt_util.utcnow()
    assert coordinator.stale is False

    freezer.tick(timedelta(hours=1, minutes=59))  # default stale_after_hours=2
    assert coordinator.stale is False

    freezer.tick(timedelta(minutes=2))
    assert coordinator.stale is True


async def test_repair_issue_created_after_prolonged_failure_and_cleared_on_recovery(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    freezer.move_to("2026-07-09T10:00:00+00:00")
    entry = _hub_with_fingrid(hass)
    subentry = _fingrid_subentry(entry)
    coordinator = FingridCoordinator(hass, entry, subentry)
    issue_id = f"{ISSUE_SOURCE_STALE}_{subentry.subentry_id}"
    coordinator.last_successful_fetch = dt_util.utcnow() - timedelta(hours=1)
    coordinator._points = [_point(9)]

    aioclient_mock.get(FINGRID_DATA_URL, status=500)
    await coordinator.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None  # not stale long enough

    # stale_after_hours(2) * STALE_REPAIR_FACTOR(3) = 6h past last_successful_fetch.
    freezer.tick(timedelta(hours=7))
    aioclient_mock.clear_requests()
    aioclient_mock.get(FINGRID_DATA_URL, status=500)
    await coordinator.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    aioclient_mock.clear_requests()
    aioclient_mock.get(FINGRID_DATA_URL, json=_FINGRID_PAYLOAD)
    await coordinator.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_fmi_coordinator_fetch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _hub_with_fmi(hass)
    subentry = _fingrid_subentry(entry)
    coordinator = FmiCoordinator(hass, entry, subentry)
    aioclient_mock.get(
        FMI_FORECAST_URL,
        json={"series": [{"data": [[1783594800000.0, 1.0], [1783598400000.0, 1.2]]}]},
    )

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert len(coordinator.points) == 2


# ── Combined coordinator: merge + reactive recompute + aggregation ──────────


def _combined_setup(hass: HomeAssistant, *, gap_fill: bool = True, with_secondary: bool = True):
    subentries = [
        ConfigSubentryData(
            subentry_type=SUBENTRY_TYPE_FINGRID,
            title="Fingrid",
            unique_id=None,
            data={CONF_API_KEY: "unused"},
        ),
        ConfigSubentryData(subentry_type=SUBENTRY_TYPE_FMI, title="FMI", unique_id=None, data={}),
        ConfigSubentryData(
            subentry_type=SUBENTRY_TYPE_COMBINED, title="Combined", unique_id=None, data={}
        ),
    ]
    entry = MockConfigEntry(
        domain=DOMAIN, data={"name": "Hub"}, unique_id=DOMAIN, subentries_data=subentries
    )
    entry.add_to_hass(hass)
    fingrid_subentry = next(
        s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_FINGRID
    )
    fmi_subentry = next(
        s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_FMI
    )
    combined_subentry = next(
        s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_COMBINED
    )
    # Fill in the combined subentry's data now that the source subentry ids
    # are known (they're assigned when added to the entry above).
    data = {CONF_PRIMARY: fingrid_subentry.subentry_id, CONF_GAP_FILL: gap_fill}
    if with_secondary:
        data[CONF_SECONDARY] = fmi_subentry.subentry_id
    hass.config_entries.async_update_subentry(entry, combined_subentry, data=data)
    combined_subentry = entry.subentries[combined_subentry.subentry_id]

    primary = FingridCoordinator(hass, entry, fingrid_subentry)
    secondary = FmiCoordinator(hass, entry, fmi_subentry) if with_secondary else None
    combined = CombinedCoordinator(hass, entry, combined_subentry, primary, secondary, gap_fill)
    return primary, secondary, combined


async def test_combined_merges_primary_and_secondary(hass: HomeAssistant) -> None:
    primary, secondary, combined = _combined_setup(hass)
    primary._points = [_point(10, 100.0)]
    secondary._points = [_point(11, 200.0)]

    combined.start()

    assert combined.points == [_point(10, 100.0), _point(11, 200.0)]


async def test_combined_reacts_to_source_listener(hass: HomeAssistant) -> None:
    primary, secondary, combined = _combined_setup(hass)
    primary._points = [_point(10, 100.0)]
    secondary._points = []
    combined.start()
    assert combined.points == [_point(10, 100.0)]

    primary._points = [_point(10, 111.0)]
    primary.async_update_listeners()

    assert combined.points == [_point(10, 111.0)]


async def test_combined_passthrough_without_secondary(hass: HomeAssistant) -> None:
    primary, _secondary, combined = _combined_setup(hass, with_secondary=False)
    primary._points = [_point(10, 100.0)]

    combined.start()

    assert combined.points == [_point(10, 100.0)]


async def test_combined_last_successful_fetch_and_stale_aggregation(hass: HomeAssistant) -> None:
    primary, secondary, combined = _combined_setup(hass)
    now = dt_util.utcnow()
    primary.last_successful_fetch = now - timedelta(hours=1)
    secondary.last_successful_fetch = now - timedelta(minutes=5)

    assert combined.last_successful_fetch == secondary.last_successful_fetch
    # Neither source is stale (default stale_after_hours=2) => combined isn't.
    assert combined.stale is False

    primary.last_successful_fetch = None
    secondary.last_successful_fetch = None
    assert combined.last_successful_fetch is None
    assert combined.stale is True  # both stale => combined stale


async def test_combined_shutdown_unsubscribes_listeners(hass: HomeAssistant) -> None:
    primary, secondary, combined = _combined_setup(hass)
    combined.start()
    assert len(primary._listeners) == 1
    assert len(secondary._listeners) == 1

    await combined.async_shutdown()

    assert len(primary._listeners) == 0
    assert len(secondary._listeners) == 0
