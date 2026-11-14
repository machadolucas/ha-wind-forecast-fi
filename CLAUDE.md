# CLAUDE.md — Wind Forecast Finland

Working notes for AI agents and future-me. Read this before changing code.

**Status (beta):** all features below are implemented and covered by tests
(run them — see Dev workflow). Built incrementally per the approved plan at
`~/.claude/plans/in-home-assistant-i-composed-newell.md` (the *why*; this file
is the *how*). The locked test environment uses Python 3.14.10 and
HA 2026.11.1; the runtime minimum remains in `hacs.json`. See the README
for the temporary test-plugin compatibility boundary.

## What this is

A Home Assistant custom integration (`wind_forecast_fi`) that publishes
Finland wind-power generation forecasts from Fingrid dataset 245 and the FMI
public CDN feed, plus an optional combined/gap-filled view, as cached,
never-go-empty entities in one canonical MW schema. It replaces two REST
template sensors (`sensor.finland_wind_forecast_average{,_fmi}`) that had no
cache, mislabelled units, and two different attribute shapes. The main
consumer is [`ha-load-need-predictor`](https://github.com/machadolucas/ha-load-need-predictor)
(`forecast_source.py:async_wind_series_gw`); `ha-load-scheduler` never touches
wind directly.

## Architecture

- **Hub config entry** (`async_step_user`) — a singleton (`unique_id =
  DOMAIN`); holds no forecast logic itself, just the name.
- **One `ConfigSubentry` per source** (`fingrid`, `fmi`) plus an optional
  `combined` subentry that merges them. Each subentry type is a singleton
  within the hub and shares one `init` step between add and reconfigure
  (`_clean()` + `suggested_value` prefill — the sibling pattern). The
  `ConfigSubentry` API is relatively new; the `homeassistant` floor in
  `hacs.json` tracks it.
- **One device per subentry** (`DeviceInfo(identifiers={(DOMAIN,
  subentry_id)}, entry_type=SERVICE)`), named after the subentry's title
  (`Fingrid` / `FMI` / `Combined` by default) — `entity.py`'s
  `WindForecastEntity` base builds it once for all three entity classes.

### Modules (`custom_components/wind_forecast_fi/`)

| File | Role | HA? |
|---|---|---|
| `models.py` | **Pure** data model: `ForecastPoint`, `merge_forecasts`, `current_value`, `next_24h_stats`, `trim_past`, `to_attributes` (the canonical-schema builder), Store (de)serialisation | no |
| `fingrid.py` | Pure `parse_fingrid_response` + thin `async_fetch_fingrid` (aiohttp only, no `homeassistant` import) | no* |
| `fmi.py` | Pure `parse_fmi_response` (GW→MW, end-time synthesis from the median gap) + thin `async_fetch_fmi` | no* |
| `const.py` | Domain, config keys, defaults, storage/repair constants | no |
| `persistence.py` | `ForecastStore`: one `Store` per source subentry | yes |
| `coordinator.py` | `SourceCoordinator` (base) → `FingridCoordinator`/`FmiCoordinator`; `CombinedCoordinator` (listener-driven, no fetch of its own) | yes |
| `config_flow.py` | Hub flow + `fingrid`/`fmi`/`combined` subentry wizards, all reconfigurable | yes |
| `entity.py` | `WindForecastEntity` base (device info, unique_id, translation key) | yes |
| `sensor.py` | `WindForecastSensor` (main) + `WindForecastNext24hSensor` | yes |
| `binary_sensor.py` | `WindForecastStaleBinarySensor` | yes |
| `__init__.py` | Wires coordinators per subentry, seeds from Store, forwards platforms, full-reload on subentry mutation | yes |
| `diagnostics.py` | Redacted per-subentry config + health dump | yes |

*`fingrid.py`/`fmi.py` import `aiohttp` but never `homeassistant` — they're
HA-free by the same convention as the pure modules, just not loaded via the
fully-standalone `importlib` trick `models.py` uses (see Dev workflow below).

## Canonical schema contract (read before touching `models.to_attributes`)

`models.to_attributes()` is the **single place** the `forecast` attribute
dict is built — every sensor (fingrid/fmi/combined) renders it by calling
this function, and a contract test (`tests/test_models.py`) pins the exact
shape:

```jsonc
{
  "forecast": [{"start": "…", "end": "…", "value": 900.0}, …],  // MW, tz-aware ISO, [start, end)
  "unit": "MW",
  "source": "fingrid" | "fmi" | "combined",
  "last_successful_fetch": "…" | null,
  "stale": false,
  "horizon_start": "…" | null,
  "horizon_end": "…" | null
}
```

Do not let any entity build this dict inline — new fields or shape changes go
into `to_attributes` (and its contract test) first, then every sensor picks
them up automatically. `_unrecorded_attributes = frozenset({"forecast"})` on
`WindForecastSensor` keeps the bulky list out of the recorder; the *state*
(current-hour MW value) still gets long-term statistics, which is what lets
downstream fitting code (`ha-load-need-predictor`) keep reading the state/LTS
path unchanged.

## The never-go-empty coordinator contract (read before touching `coordinator.py`)

- **Startup**: `async_seed_from_store()` loads the persisted snapshot
  *before* the first refresh, so entities are populated even if the network
  is down at boot. `__init__.py.async_setup_entry` calls plain
  `async_refresh()` (never raises) rather than
  `async_config_entry_first_refresh()` (which raises `ConfigEntryNotReady`
  and would take the *whole hub* — including a healthy sibling source — down
  with it). One dead source with no cache just leaves its own entities
  unavailable; it must never block the hub.
- **On fetch failure** (`SourceCoordinator._async_update_data`): increment
  the failure counter, update the repair issue, and — if any cached points
  exist — log a warning and return them **unchanged** (the coordinator stays
  "successful" as far as `DataUpdateCoordinator` is concerned). Only raise
  `UpdateFailed` when there is truly nothing to serve.
- **On fetch success**: reset the failure counter, set
  `last_successful_fetch = utcnow()`, trim points older than
  `RETENTION_PAST_HOURS` (`models.trim_past`), debounce-save to `Store`
  (`SAVE_DELAY = 10s`), clear the repair issue.
- **`stale`/`last_successful_fetch` are decoupled from HA's own
  `last_update_success`** — that's the entire point: a source that's
  currently failing but still serving cached points keeps
  `last_update_success = True` (so `DataUpdateCoordinator`'s "still failing"
  early-return never triggers and listeners keep getting notified), while
  `stale` tracks elapsed real time since the last *actual* success. This is
  what lets `binary_sensor.*_forecast_data_stale` and the main sensor's
  `available` disagree on purpose (see `binary_sensor.py`'s docstring for the
  full timing walkthrough).
- **Entity availability** (`sensor.py`): `available` iff any cached point has
  `end > now` — deliberately *not* the inherited `CoordinatorEntity.available`
  for the same reason as above.
- **Repair issue per source** (`_update_repair_issue`):
  `ir.async_create_issue(…, f"source_stale_{subentry_id}", is_fixable=False,
  severity=WARNING)` once failing for `stale_after_hours ×
  STALE_REPAIR_FACTOR` (factor `3`) with `_failures > 0`; deleted as soon as
  that's no longer true. Mirrors `LoadSchedulerCoordinator._update_price_issue`
  in `ha-load-scheduler`.
- **`CombinedCoordinator`** does no fetching (`update_interval=None`);
  `start()` subscribes to its source coordinator(s)'
  `async_add_listener(self.async_recompute)` and recomputes via
  `models.merge_forecasts` + `async_set_updated_data`. `stale` is true only
  once *every* contributing source is stale; `last_successful_fetch` is the
  max across them. No secondary configured → clean passthrough.
- **Persistence**: `.storage/wind_forecast_fi.<subentry_id>`, one file per
  *source* subentry (not per combined — it has nothing of its own to
  persist), `STORAGE_VERSION = 1`. Removing a subentry does **not** clean up
  its Store file (mirrors `ha-load-scheduler`/`ha-load-need-predictor` — the
  reload listener only sees the *new* subentry set, so diffing to find what
  was removed isn't justified by an orphaned few-KB file).

## Pure vs. HA test split

- `tests/` (pure, plain `pytest`, no `homeassistant` import at all) —
  `test_models.py`, `test_fingrid.py`, `test_fmi.py`. `tests/ha/` (HA-side,
  `pytest-homeassistant-custom-component`) — config flow, coordinator,
  sensor, binary_sensor, diagnostics, init.
- **Keep `models.py`/`fingrid.py`/`fmi.py` HA-free** — that's what makes the
  pure suite runnable with `pytest` and `aiohttp`, without the HA dependency tree.
- Pure parser tests use an isolated `_wind_forecast_unit` package whose path
  points to the integration directory. Their relative `.models` imports resolve
  there without executing the HA-bound `__init__.py` or replacing real integration
  modules used by the HA suite. `test_models.py` loads its dependency-free module
  directly as `wf_models`.
- `__init__.py`'s own docstring explains the mirror-image constraint on the
  *production* side: `.coordinator` (which imports `.fingrid`/`.fmi`) is
  imported lazily *inside* `async_setup_entry`, not at module level, so that
  importing `fingrid.py`/`fmi.py` standalone in tests never has to resolve a
  circular import back through the package `__init__.py`.

## Dev workflow

```bash
uv sync --locked
uv run pytest --cov=custom_components/wind_forecast_fi --cov-fail-under=90
uv run ruff check . && uv run ruff format --check .
```

CI (`.github/workflows/test.yaml` + `validate.yaml`) runs the same pytest/ruff
gate plus `hassfest` and `hacs/action`. Both are tracked in git (the push
token here has `workflow` scope, unlike the sibling repos where the CI files
are git-ignored) — check `gh run list --repo machadolucas/ha-wind-forecast-fi`
after every push.

## Releasing a version

A release is the **version bump + commit on `main` + push + a GitHub
release** — all four. HACS reads the version from `manifest.json` **and**
picks up GitHub releases, so both must move together.

1. Bump `"version"` in `custom_components/wind_forecast_fi/manifest.json`
   (SemVer: **minor** for a feature, **patch** for a fix/tweak).
2. Commit **directly to `main`** (every prior release is a direct main
   commit, not a PR/branch). Message convention: `vX.Y.Z: short description`.
3. `git push origin main`.
4. `gh release create vX.Y.Z --title "vX.Y.Z — short human description"
   --generate-notes` — the **GitHub release creates the tag**; there is no
   separate `git tag` step.

Gotcha: local `git tag` lags behind because release tags are created
server-side by `gh release create` and never fetched. **Use `gh release list`
to see the real latest version, not `git tag`.**

## Conventions

- Comment the *why*, not the *what*; match the density in `coordinator.py`
  and `models.py`.
- New forecast-model behaviour (a new stat, a new merge rule) goes into
  `models.py` as a tested pure function first, then gets wired into the
  coordinator/sensor.
- Don't commit secrets; the Fingrid API key lives in the config subentry
  (redacted in diagnostics via `async_redact_data`) and runtime cache lives in
  `.storage/` (both covered by HA backups).
- `strings.json` and `translations/en.json` must stay byte-identical — update
  both in the same commit. hassfest also rejects URLs inside translation
  strings (`error`/`data_description` text) — link out via plain prose
  ("Fingrid's open data portal") instead of a bare `https://…` in the string,
  and put clickable links only in the README.
- `_unrecorded_attributes = frozenset({"forecast"})` on `WindForecastSensor`
  — any *other* future bulky attribute added to a sensor needs the same
  treatment; don't let the recorder DB absorb per-bucket lists.

## Consumer contract note

[`ha-load-need-predictor`](https://github.com/machadolucas/ha-load-need-predictor)
≥ v0.7.0's `forecast_source.py:async_wind_series_gw` reads
`attributes["forecast"]` (`[{start, end, value}]`, MW) first, converting
`value / 1000` → GW; if that key is absent (older config still pointed at the
legacy REST sensors) it falls back to the old `series[0].data` `[epoch_ms,
GW]` reader. Any change to `to_attributes`'s shape is a breaking change for
that fallback-free path — bump both repos together and check that reader
before renaming/removing `forecast`'s keys.

## Branding / icon

Home Assistant 2026.3 and newer load the bundled integration icon from
`custom_components/wind_forecast_fi/brand/icon.png` (256 × 256) and
`icon@2x.png` (512 × 512). The high-resolution artwork is kept at
`brands/icon-master.png`; `brands/icon.svg` remains as legacy artwork.
The root `brands/` directory is not the integration's runtime asset location.

Local branding requires no submission to `home-assistant/brands` and no
manifest icon override. Older Home Assistant versions retain their existing
icon fallback; this cosmetic update does not raise the supported HA minimum
in `hacs.json`. The current HACS brand validator accepts the bundled
`brand/icon.png`, so the validation workflow no longer ignores `brands`.
