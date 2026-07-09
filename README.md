# Wind Forecast Finland

[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration (`wind_forecast_fi`) that publishes
Finland's wind-power generation forecast from two independent sources —
[Fingrid](https://data.fingrid.fi) dataset 245 (15-minute buckets, needs a
free API key) and the Finnish Meteorological Institute's public wind-forecast
CDN feed (hourly, no key) — plus an optional **combined** sensor that merges
them. All three publish the same MW schema.

> **Status: beta.** Install as a HACS custom repository (below) — not yet in
> the HACS default store. Backed by an extensive test suite.

## Why

Both sources are otherwise just JSON over HTTP, and it's tempting to reach
for a couple of REST/template sensors. This integration exists because that
approach has three sharp edges in practice:

- **REST sensors have no cache.** Any network blip, API outage, or HA restart
  during one leaves the sensor empty until the next successful poll — exactly
  when a scheduler consuming the forecast most needs a number to plan around.
  Every source here persists its last-good snapshot to a `Store` and reloads
  it at startup, so entities are populated even if the network is down at
  boot, and a fetch failure serves the cached forecast rather than emptying
  the entity.
- **No staleness visibility.** A REST sensor that's quietly failing looks
  identical to one that's fine — same last value, no signal anything is
  wrong. Each source publishes a diagnostic `stale` binary sensor and raises
  an HA repair issue once it's been failing for a while, so a dead API key or
  a downed endpoint surfaces instead of silently going stale.
- **Inconsistent units and shapes.** Fingrid publishes MW; FMI publishes GW
  and expects you to remember the ×1000. Hand-rolled REST sensors tend to
  mislabel units too (`MWh` instead of `MW`). This integration normalises
  both sources to one canonical `forecast` attribute schema (MW, tz-aware
  ISO 8601 buckets) so anything consuming it — a dashboard card, a template,
  another integration — reads one shape regardless of source.

## Installation

### HACS (custom repository)

1. HACS → ⋮ → *Custom repositories* → add
   `https://github.com/machadolucas/ha-wind-forecast-fi`, category
   **Integration**.
2. Install **Wind Forecast Finland**, then restart Home Assistant.
3. *Settings → Devices & Services → Add Integration → Wind Forecast Finland.*

### Manual

Copy `custom_components/wind_forecast_fi` into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

The integration is a **singleton hub** (only one "Finland" per HA instance) —
add it once, then attach one or more **subentries** from the integration's
page ("+ Add") for the sources you want:

1. **Add the hub** (*Add Integration → Wind Forecast Finland*) — name only.
2. **Add a Fingrid source** — needs a free API key from
   [data.fingrid.fi](https://data.fingrid.fi): register, then create an API
   key with access to open datasets (dataset 245 needs no special
   permission). The key is validated with a small live test-fetch before the
   subentry is saved.
3. **Add an FMI source** — public endpoint, no key.
4. **Add a Combined view** (optional) — picks one source as primary and,
   optionally, fills true gaps in it from a secondary source. Requires at
   least one Fingrid/FMI source to already exist.

Each subentry type is a singleton within the hub (one Fingrid, one FMI, one
Combined) and is reconfigurable in place (⋮ → *Reconfigure* on the subentry)
without re-entering every field.

| Option | Applies to | Default | Notes |
|---|---|---|---|
| API key | Fingrid | — (required) | Rejected (401/403) → `invalid_api_key`; unreachable → `cannot_connect`. |
| Update interval | Fingrid, FMI | 15 min | Floor 5 min — both sources refresh their own data on a similar cadence, so polling faster just wastes rate limit. |
| Stale after | Fingrid, FMI | 2 h | Hours without a successful fetch before `stale` flips and the diagnostic binary sensor turns on. |
| Lookback window | Fingrid | 12 h | Hours of already-past buckets kept fetched/displayed, for dashboard context. |
| Forecast horizon | Fingrid | 74 h | Hours ahead fetched (Fingrid typically publishes 36–74 h depending on the run). |
| Primary source | Combined | — (required) | Which source's forecast wins on overlapping hours. Picked from the hub's existing Fingrid/FMI subentries. |
| Secondary source | Combined | — (optional) | Only fills hours the primary doesn't cover. |
| Fill gaps from secondary | Combined | on | Off → clean passthrough of the primary only. |

A repair issue (`source_stale_<subentry_id>`) is raised once a source has been
failing for `stale_after_hours × 3` with no successful fetch, and clears
automatically on the next successful fetch.

## Entities

One HA **device** per subentry (Fingrid, FMI, Combined), each with the same
three entities. Entity IDs are the HA-generated default
(`has_entity_name` + the subentry's default title) — rename the device to get
different IDs.

| Subentry (default title) | `sensor` (main) | `sensor` (next 24 h) | `binary_sensor` (stale) |
|---|---|---|---|
| Fingrid | `sensor.fingrid_wind_forecast` | `sensor.fingrid_wind_forecast_next_24_h` | `binary_sensor.fingrid_forecast_data_stale` |
| FMI | `sensor.fmi_wind_forecast` | `sensor.fmi_wind_forecast_next_24_h` | `binary_sensor.fmi_forecast_data_stale` |
| Combined | `sensor.combined_wind_forecast` | `sensor.combined_wind_forecast_next_24_h` | `binary_sensor.combined_forecast_data_stale` |

- **`sensor.<source>_wind_forecast`** — the main sensor. State = the current
  hour's forecast value in MW (falls back to the earliest future bucket if
  the current hour has a gap; unknown only when nothing with `end > now`
  exists). Attributes carry the full canonical schema (below). `device_class:
  power`, `unit_of_measurement: MW`, `state_class: measurement` — deliberately
  the same unit the legacy REST sensors' state/statistics used, so downstream
  consumers that read long-term statistics (e.g. `ha-load-need-predictor`)
  need no changes there.
- **`sensor.<source>_wind_forecast_next_24_h`** — state = average MW over the
  next 24 h; attributes `min` / `min_time` / `max` / `max_time` (the extreme
  value and when it occurs in that window). Kept as attributes on this one
  sensor rather than four separate entities, to avoid entity sprawl.
- **`binary_sensor.<source>_forecast_data_stale`** — `device_class: problem`,
  diagnostic category. On when the source's `stale` flag is set (see below).
  Deliberately decoupled from the main sensor's `available` state: this
  entity reports *health*, so it stays available (and reports `on`) even when
  a source has zero cached data — "no data" is exactly the failure this
  sensor exists to surface.

### The canonical `forecast` attribute

Every main sensor (Fingrid, FMI, Combined) publishes the identical shape —
built in one place (`models.to_attributes`) and contract-tested, so a
consumer written against one source works unchanged against any of them:

```jsonc
{
  "forecast": [
    {"start": "2026-07-09T12:00:00+00:00", "end": "2026-07-09T12:15:00+00:00", "value": 900.0},
    {"start": "2026-07-09T12:15:00+00:00", "end": "2026-07-09T12:30:00+00:00", "value": 950.0}
  ],
  "unit": "MW",
  "source": "fingrid",
  "last_successful_fetch": "2026-07-09T12:05:03+00:00",
  "stale": false,
  "horizon_start": "2026-07-09T12:00:00+00:00",
  "horizon_end": "2026-07-09T12:30:00+00:00"
}
```

- `value` is always MW; `start`/`end` are tz-aware ISO 8601, half-open
  `[start, end)` buckets, ascending.
- `source` is `"fingrid"`, `"fmi"`, or `"combined"`.
- `stale`/`last_successful_fetch` mirror the diagnostic binary sensor's
  underlying state (for a combined view, `stale` is true only once *every*
  contributing source is stale, and `last_successful_fetch` is the max across
  them).
- `forecast` is excluded from the recorder (`_unrecorded_attributes`) — it's a
  bulky per-bucket list that would otherwise bloat the history DB; the *state*
  still gets long-term statistics.

Anything that can read entity attributes can consume this — a Jinja template
(`state_attr('sensor.combined_wind_forecast', 'forecast')`), a card like
[ApexCharts Card](https://github.com/RomRider/apexcharts-card) or
[Plotly Graph Card](https://github.com/dbuezas/lovelace-plotly-graph-card)
(see below), or another integration.
[`ha-load-need-predictor`](https://github.com/machadolucas/ha-load-need-predictor)
≥ v0.7.0 reads this schema natively (with a fallback to the legacy REST
sensors' `series` shape for anyone still on the old sensors).

## Staleness & repair issues

Two independent signals, both driven by `last_successful_fetch` rather than
HA's own `last_update_success` (which the never-go-empty contract keeps
`True` across a transient failure as long as cached points still exist):

- **`stale` attribute / binary sensor** — flips as soon as
  `now - last_successful_fetch > stale_after_hours`. A source with *no*
  successful fetch yet is always stale. This is meant to be glanceable on a
  dashboard.
- **Repair issue** (`source_stale_<subentry_id>`) — a step up in severity:
  raised only once a source has been failing for `stale_after_hours ×
  STALE_REPAIR_FACTOR` (factor is `3`, so 6 h at the default 2 h threshold)
  *and* has recorded at least one failure. This wider margin avoids paging you
  over a single missed poll while still catching a genuinely dead key/endpoint.
  Deletes itself on the next successful fetch.

## Dashboard card

A simplified [Plotly Graph Card](https://github.com/dbuezas/lovelace-plotly-graph-card)
replacing the two-shape REST-sensor card this integration was built to
retire. Both traces now read the same canonical `forecast` attribute through
one identical filter function — no GW×1000 conversion, no per-source shape
differences — just the entity swapped per trace. Still keeps the "Now" line,
the day-boundary lines, and the day-name annotations from the original card.

```yaml
type: custom:plotly-graph
hours_to_show: 84h
time_offset: 72h
layout:
  title:
    xanchor: left
    yanchor: bottom
    x: 0.02
    "y": 0.92
    text: Finland wind power generation forecast
    font:
      size: 14
  height: 180
  showlegend: false
  paper_bgcolor: rgba(0,0,0,0.3)
  plot_bgcolor: transparent
  margin:
    t: 25
    r: 40
    l: 10
    b: 35
  annotations:
    - x: $ex { return new Date() }
      yref: paper
      "y": 1
      text: Now
      textangle: 270
      showarrow: false
      opacity: 0.75
      xanchor: right
      font:
        color: white
        shadow: "-1px 1px 1px black"
    - x: $ex new Date().setHours(23, 59, 59) - 86400000
      xref: x
      yref: paper
      "y": 1
      textangle: 270
      showarrow: false
      opacity: 0.55
      xanchor: right
      font:
        color: white
        shadow: "-1px 1px 1px black"
      text: |-
        $ex {
          const date = new Date(new Date().setHours(12, 0, 0));
          const weekday = date.toLocaleDateString("en-GB", { weekday: "short" });
          const dateStr = date.toLocaleDateString("fi-FI", { day: "numeric", month: "numeric" });
          return `${weekday} ${dateStr}`;
        }
    - x: $ex new Date().setHours(23, 59, 59)
      xref: x
      yref: paper
      "y": 1
      textangle: 270
      showarrow: false
      opacity: 0.55
      xanchor: right
      font:
        color: white
        shadow: "-1px 1px 1px black"
      text: |-
        $ex {
          const date = new Date(new Date().setHours(12, 0, 0) + 86400000);
          const weekday = date.toLocaleDateString("en-GB", { weekday: "short" });
          const dateStr = date.toLocaleDateString("fi-FI", { day: "numeric", month: "numeric" });
          return `${weekday} ${dateStr}`;
        }
    - x: $ex new Date().setHours(23, 59, 59) + 86400000
      xref: x
      yref: paper
      "y": 1
      textangle: 270
      showarrow: false
      opacity: 0.55
      xanchor: right
      font:
        color: white
        shadow: "-1px 1px 1px black"
      text: |-
        $ex {
          const date = new Date(new Date().setHours(12, 0, 0) + 2 * 86400000);
          const weekday = date.toLocaleDateString("en-GB", { weekday: "short" });
          const dateStr = date.toLocaleDateString("fi-FI", { day: "numeric", month: "numeric" });
          return `${weekday} ${dateStr}`;
        }
  xaxis:
    fixedrange: true
    nticks: 12
    showgrid: false
    tickformat: "%-H:%M"
  yaxis:
    side: right
    nticks: 5
    tickformat: ~s
    fixedrange: true
    range: $ex [0, 9000]
  yaxis9:
    visible: false
    fixedrange: true
    autorangeoptions:
      clipmax: 1
      clipmin: 0
config:
  displayModeBar: false
  scrollZoom: false
  responsive: true
entities:
  - entity: sensor.fmi_wind_forecast
    type: scatter
    mode: lines
    name: Generation forecast (FMI)
    filters:
      - fn: |-
          ({ meta }) => ({
            xs: (meta.forecast ?? []).map((p) => new Date(p.start)),
            ys: (meta.forecast ?? []).map((p) => p.value),
          })
    fill: tozeroy
    fillgradient:
      colorscale: Cividis
      type: vertical
      start: 0
      stop: 4500
    line:
      color: deepskyblue
      width: 1
      shape: spline
    yhoverformat: d
  - entity: sensor.fingrid_wind_forecast
    type: scatter
    mode: lines
    name: Generation forecast
    filters:
      - fn: |-
          ({ meta }) => ({
            xs: (meta.forecast ?? []).map((p) => new Date(p.start)),
            ys: (meta.forecast ?? []).map((p) => p.value),
          })
    fill: tozeroy
    fillgradient:
      colorscale: Cividis
      type: vertical
      start: 0
      stop: 4500
    line:
      color: gold
      width: 1
      shape: spline
    yhoverformat: d
  - entity: ""
    name: Now
    yaxis: y9
    opacity: 0.75
    line:
      width: 1
      dash: solid
      color: white
    x: $ex [Date.now(), Date.now()]
    "y":
      - -1
      - 2
  - entity: ""
    name: Days lines
    yaxis: y9
    opacity: 0.15
    line:
      width: 1
      dash: solid
      color: white
    hovertext: ""
    hoverinfo: skip
    x: |-
      $ex [
        new Date().setHours(23, 59, 59) - 86400000,
        new Date().setHours(23, 59, 59) - 86400000,
        new Date().setHours(23, 59, 59),
        new Date().setHours(23, 59, 59),
        new Date().setHours(23, 59, 59) + 86400000,
        new Date().setHours(23, 59, 59) + 86400000,
        new Date().setHours(23, 59, 59) + 2*86400000,
        new Date().setHours(23, 59, 59) + 2*86400000
      ]
    "y":
      - -1
      - 2
      - -1
      - 2
      - -1
      - 2
      - -1
      - 2
```

Swap the two `sensor.*` entities for a Combined-only view (drop one trace),
or point both at `sensor.combined_wind_forecast` and
`sensor.fingrid_wind_forecast`/`sensor.fmi_wind_forecast` to compare a single
source against the merged view.

## Development

```bash
uv venv --python 3.13 .venv313
uv pip install --python .venv313/bin/python -r requirements_test.txt
.venv313/bin/python -m pytest
.venv313/bin/ruff check . && .venv313/bin/ruff format --check .
```

The forecast model and both source parsers
([`models.py`](custom_components/wind_forecast_fi/models.py),
[`fingrid.py`](custom_components/wind_forecast_fi/fingrid.py),
[`fmi.py`](custom_components/wind_forecast_fi/fmi.py)) have **no Home
Assistant dependency** and are tested in isolation.

See [`CLAUDE.md`](CLAUDE.md) for the architecture and the never-go-empty /
canonical-schema contracts.

## License

MIT © Lucas Machado
