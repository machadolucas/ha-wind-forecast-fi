"""Store-backed persistence for one source's forecast snapshot.

One ``Store`` file per source subentry (fingrid / fmi) under ``.storage/``, so
each source's cached forecast survives a restart independently and is part of
Home Assistant backups. The coordinator treats this as the seed for the
never-go-empty contract: entities are populated at startup even if the network
is down (see ``coordinator.SourceCoordinator.async_seed_from_store``).
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN, SAVE_DELAY, STORAGE_VERSION


class ForecastStore:
    """Thin wrapper over ``Store`` keyed per source subentry."""

    def __init__(self, hass: HomeAssistant, subentry_id: str) -> None:
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{subentry_id}")

    async def async_load(self) -> dict[str, Any] | None:
        """Return the saved snapshot (``{"points": [...], "last_successful_fetch": ...}``).

        ``None`` when nothing has ever been saved for this subentry.
        """
        return await self._store.async_load()

    @callback
    def async_schedule_save(self, snapshot: dict[str, Any]) -> None:
        """Debounced save of ``snapshot`` (built by the caller via ``models``).

        ``snapshot`` is captured by value now (it already reflects the trimmed
        points at call time), then handed to ``Store`` as a zero-arg callable —
        matching the ``async_delay_save`` API without needing the caller to
        pass a closure of its own.
        """
        self._store.async_delay_save(lambda: snapshot, SAVE_DELAY)

    async def async_save_now(self, snapshot: dict[str, Any]) -> None:
        """Write immediately, bypassing the debounce (tests / explicit flush)."""
        await self._store.async_save(snapshot)

    async def async_remove(self) -> None:
        """Delete the Store file (subentry removal)."""
        await self._store.async_remove()
