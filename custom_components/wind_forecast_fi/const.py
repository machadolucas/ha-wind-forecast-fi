"""Constants for the Wind Forecast Finland integration."""

from __future__ import annotations

DOMAIN = "wind_forecast_fi"

# ── Subentry types (hub + one subentry per source, plus a combined one) ─────
SUBENTRY_TYPE_FINGRID = "fingrid"
SUBENTRY_TYPE_FMI = "fmi"
SUBENTRY_TYPE_COMBINED = "combined"

# ── Config keys (hub) ────────────────────────────────────────────────────────
CONF_NAME = "name"
DEFAULT_NAME = "Wind Forecast Finland"

# ── Config keys (fingrid / fmi source subentries) ────────────────────────────
CONF_API_KEY = "api_key"
CONF_UPDATE_INTERVAL = "update_interval"  # minutes
CONF_STALE_AFTER_HOURS = "stale_after_hours"
CONF_LOOKBACK_HOURS = "lookback_hours"
CONF_HORIZON_HOURS = "horizon_hours"

# ── Config keys (combined subentry) ──────────────────────────────────────────
CONF_PRIMARY = "primary"
CONF_SECONDARY = "secondary"
CONF_GAP_FILL = "gap_fill"

# ── Defaults ──────────────────────────────────────────────────────────────────
UPDATE_INTERVAL_MINUTES_FLOOR = 5
DEFAULT_UPDATE_INTERVAL_MINUTES = 15
DEFAULT_STALE_AFTER_HOURS = 2
DEFAULT_LOOKBACK_HOURS = 12
DEFAULT_HORIZON_HOURS = 74
DEFAULT_GAP_FILL = True

# ── Persistence (Store under .storage/, keyed per subentry) ─────────────────
STORAGE_VERSION = 1
SAVE_DELAY = 10  # seconds — debounce Store writes
RETENTION_PAST_HOURS = 12  # trim points whose bucket ended before now - this

# ── Repair issues ─────────────────────────────────────────────────────────────
# A source's repair issue is raised once it has been failing for at least
# stale_after_hours * STALE_REPAIR_FACTOR with no successful fetch.
STALE_REPAIR_FACTOR = 3
ISSUE_SOURCE_STALE = "source_stale"
