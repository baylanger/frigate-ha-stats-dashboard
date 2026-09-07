# Frigate Detection Stats Home Assistant Dashboard — Install Guide

This sets up two Home Assistant bar-graph dashboards backed by Frigate's
event history:

1. **Hourly detections** — bus/car/motorcycle/person counts per hour, last 12h
2. **Passerby probability** — likelihood of a detection per 5-min bucket,
   split weekday vs. weekend, based on the last 3 weeks

Both are computed by a single pyscript file and rendered with the
ApexCharts Card.

---

## Prerequisites

### 1. Frigate: retain event metadata for ≥ 21 days

The probability graph needs 3 weeks of event *rows*, not clips. In your
Frigate config, make sure event retention covers at least 21 days, e.g.:

```yaml
record:
  events:
    retain:
      default: 21
```

(Exact key depends on your Frigate version — check your current
`config.yml` under `record.events.retain`. Video clip retention can stay
shorter; only the event metadata needs to survive 21 days.)

### 2. HACS installed in Home Assistant

Needed to install the ApexCharts Card below. If you don't have HACS yet:
https://hacs.xyz/docs/use/download/download/

### 3. pyscript integration

The detection-processing logic runs as a pyscript service.

- HACS → Integrations → search **"pyscript"** → install
- Restart Home Assistant
- Settings → Devices & Services → Add Integration → **pyscript**
  You'll be asked three questions during setup — answer:
  - **Allow all imports?** → **Yes** (the script does `import requests`,
    which isn't in pyscript's default safe-import allowlist)
  - **Access hass as a global variable?** → **No** (the script uses
    pyscript's own `state.set(...)` helper rather than the raw `hass`
    object — calling `hass.states.set()` directly from a pyscript
    trigger throws `RuntimeError: Cannot be called from within the
    event loop`, since that method expects to be called from a worker
    thread, not pyscript's event-loop context)
  - **Use legacy decorators?** → **No** (the script uses the current
    `@time_trigger(...)` syntax, not the legacy style)

### 4. ApexCharts Card

- HACS → Frontend → search **"apexcharts-card"** → install
- Restart Home Assistant (or reload resources) if the card isn't
  recognized in the dashboard editor

### 5. Frigate reachable from Home Assistant

Confirm HA can reach Frigate's HTTP API, e.g.:

```
curl http://<frigate-host>:5000/api/events?limit=1
```

If that doesn't return JSON, fix networking/hostname before continuing —
nothing below will work without it.

---

## Step 1 — Install the pyscript file

1. On the HA host, pyscript's folder already exists at `/config/pyscript/`
   (created automatically when the integration is installed).
2. Create a file there named `frigate-detections-stats.py` and paste in the
   full script (see `frigate-detections-stats.py` provided alongside this
   README — the parameterized version with `GRAPH_CONFIGS`).
3. Edit the top of the file:
   - Set `FRIGATE_URL` to your actual Frigate host/port.
   - `HOURLY_CRON` / `PROBABILITY_CRON` control how often each graph
     type refreshes, in standard 5-field cron syntax. Defaults:
     every 5 minutes for the hourly-style graphs, hourly for the
     probability graph.
   - The probability graph uses an **incremental** design, not a full
     rescan every run: it keeps a persisted per-day/per-bucket "hit
     set" (`pyscript.frigate_probability_daily_cache`) recording which
     5-min buckets had a detection, per day, per label. Each run only
     fetches events *since the last run* (cheap), folds them into
     today's entry, drops any day older than `days_back` from the
     cache, and recomputes weekday/weekend probabilities from the
     retained per-day data — no Frigate query needed for that last
     step. This is what makes hourly (or even more frequent)
     `PROBABILITY_CRON` reasonable despite `days_back: 21` — the
     21-day window is only fully rescanned once, to seed the cache,
     or again later if the cache goes stale (e.g. HA was down longer
     than `days_back`). The hourly graph doesn't need this — it only
     scans its `hours_back` window (12h by default) every run, which
     is already cheap regardless of `HOURLY_CRON`'s frequency. Edit
     the cron constants directly rather than the `@time_trigger(...)`
     decorators further down the file.
   - The script survives HA restarts without re-querying Frigate,
     via a cache-and-restore pattern: `state.persist()` only works on
     entities in the `pyscript.*` domain (it rejects `sensor.*`
     directly), so each real sensor's *final computed output* is
     mirrored into a matching `pyscript.frigate_hourly_cache` /
     `pyscript.frigate_probability_cache` "display cache" entity,
     that entity is what gets persisted, and a `startup`-triggered
     function copies the restored cache back onto the real sensor.
     This restores *last-known* data instantly, not a fresh pull —
     the sensors self-correct at the next scheduled/manual refresh.
     On the very first run ever (before the cache has anything in
     it), the startup function computes fresh instead of leaving the
     sensor empty — this only happens once, since after that the
     cache is always populated. Note this display-cache setup only
     supports one entry per mode (`hourly` / `probability`) — see the
     comment above `GRAPH_CONFIGS` if you add more.
   - Separately, the probability graph also persists a second,
     different-purpose cache — `pyscript.frigate_probability_daily_cache`
     — holding the raw per-day hit data described above. This one
     *is* keyed per-sensor internally, so it does support multiple
     probability entries without collision, unlike the display cache.
     Don't confuse the two: the display cache is about surviving
     restarts without a blank dashboard; the daily cache is about
     avoiding a full 21-day rescan on every refresh.
   - Review `GRAPH_CONFIGS` — the two default entries (`hourly` and
     `probability`) match the sensors used by the dashboard cards below.
     Adjust `zones`, `required_zones`, `labels`, `sub_labels`, and
     `min_score` per entry as needed.
   - Both graph modes support `bucket_min` — the bar width in minutes.
     `hourly` mode defaults to `60` (one bar per hour) if omitted; the
     default `GRAPH_CONFIGS` entry sets it to `5` to match
     `HOURLY_CRON`'s refresh cadence — these two don't have to match,
     but it's a sensible default so bars update as often as they can
     meaningfully change. `probability` mode has no default — it's
     required for that mode (the earlier examples use `5`).
4. Reload pyscript: Developer Tools → YAML → **pyscript** (or restart HA).

### Verify it's running

- Developer Tools → States → search `sensor.frigate_hourly_detections`
  and `sensor.passerby_probability`.
- `frigate_hourly_detections` updates every 15 minutes
  (`cron(*/15 * * * *)`); its state should appear within 15 min of
  reload, sooner if you manually trigger the service:
  `pyscript.frigate_refresh_hourly` / `pyscript.frigate_refresh_probability` from
  Developer Tools → Actions. Both refresh functions carry an explicit
  `@service` decorator so they always register as callable services —
  if you don't see them in the Actions picker right after a reload, try
  calling them directly in YAML mode rather than relying on the
  dropdown (which can be stale), or check the pyscript logs for a
  loading error.
- `passerby_probability` runs hourly by default — trigger it
  manually the first time so you don't wait up to an hour to see data:
  Developer Tools → Actions → run `pyscript.frigate_refresh_probability`.
  There's also `pyscript.frigate_reset_probability_cache` — run this
  (then re-run the refresh) any time you change a probability entry's
  `zones`/`min_score`/`labels`/`required_zones`, so the incremental
  cache reseeds under the new filter instead of mixing old and new
  criteria together.
- Click either sensor and check its **attributes** — you should see
  `hours`/`timestamps`/`person`/`car` arrays (hourly sensor) or
  `labels`/`timestamps`/`person_weekday`/`person_weekend` arrays
  (probability sensor). The `hours`/`labels` strings are just for
  readability here — the dashboard cards actually plot against
  `timestamps` (real millisecond values), since this apexcharts-card
  version needs an actual datetime axis to render bars correctly.
  Empty arrays usually mean the Frigate URL, zone name, or `min_score`
  is filtering out everything — double check `GRAPH_CONFIGS` and the
  `curl` test above.

---

## Step 2 — Add the dashboard cards

Edit your dashboard in YAML mode (or use "Add Card → Manual") and add:

### Hourly detections card

**Note:** each series' `legend_value` is explicitly disabled below.
Without it, apexcharts-card shows the referenced *entity's* raw
current state next to the legend name — since every series here
points at the same `sensor.frigate_hourly_detections` entity, that
would just repeat the sensor's overall `total` value under every
label, which is confusing and not what the bars actually show.

`chart.stacked: true` combines all four label types into a single bar
per time bucket, color-coded by segment, instead of separate
side-by-side bars — so a bucket with both a car and a person shows
one bar split into two colored segments rather than two thin bars
competing for the same slot.

```yaml
type: custom:apexcharts-card
header:
  title: Detections — Last 12 Hours
graph_span: 12h
apex_config:
  chart:
    type: bar
    stacked: true
  legend:
    show: true
    showForSingleSeries: true
  xaxis:
    type: datetime
    labels:
      format: "HH:mm"
series:
  - entity: sensor.frigate_hourly_detections
    name: Person
    type: column
    color: "#1E88E5"
    show:
      legend_value: false
    data_generator: |
      return entity.attributes.timestamps.map((t, i) => [t, entity.attributes.person[i]]);
  - entity: sensor.frigate_hourly_detections
    name: Car
    type: column
    color: "#E53935"
    show:
      legend_value: false
    data_generator: |
      return entity.attributes.timestamps.map((t, i) => [t, entity.attributes.car[i]]);
  - entity: sensor.frigate_hourly_detections
    name: Bicycle
    type: column
    color: "#43A047"
    show:
      legend_value: false
    data_generator: |
      return entity.attributes.timestamps.map((t, i) => [t, entity.attributes.bicycle[i]]);
  - entity: sensor.frigate_hourly_detections
    name: Motorcycle
    type: column
    color: "#FB8C00"
    show:
      legend_value: false
    data_generator: |
      return entity.attributes.timestamps.map((t, i) => [t, entity.attributes.motorcycle[i]]);
```

### Passerby probability cards

**Note:** a heatmap layout (one row per day-type, color = probability)
was tried here first, since it's arguably the cleanest fit for this
data shape. It had to be dropped — this build of apexcharts-card
throws `series_in_graph[i.seriesIndex].entity is undefined` when
`chart.type: heatmap` is used, because its internal hover/hookup logic
assumes a line/bar/area "graph" structure that heatmap doesn't
provide. Not fixable from the card config; it's a limitation of this
card version. Two separate bar cards below is the reliable fallback —
same pattern as the hourly detections card, just split so the 288
weekday/weekend buckets aren't overlapping in one chart.

**Note:** these cards compute today's date **in the browser** (via
`new Date()`) rather than relying on the sensor's stored `timestamps`
attribute. The sensor only refreshes on `PROBABILITY_CRON`'s schedule
(hourly by default), so a timestamp anchored to "today" at the time
of that run goes stale the moment the calendar rolls over — the card
would then be asking for "today" while the data is still labeled
"yesterday," and every bar gets filtered out with no error. Computing
the date client-side avoids that staleness window entirely; the
sensor's `labels` (`"HH:MM"` strings, not tied to any specific date)
are what actually drive the bars.

**Weekday:**

```yaml
type: custom:apexcharts-card
header:
  title: Street Passerby Probability — Weekday (last 3 weeks)
graph_span: 24h
span:
  start: day
apex_config:
  chart:
    type: bar
  legend:
    show: true
    showForSingleSeries: true
  xaxis:
    type: datetime
    labels:
      format: "HH:mm"
series:
  - entity: sensor.passerby_probability
    name: Weekday
    type: column
    color: "#1E88E5"
    data_generator: |
      const now = new Date();
      const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
      return entity.attributes.labels.map((l, i) => {
        const [hh, mm] = l.split(":").map(Number);
        return [startOfDay + (hh * 60 + mm) * 60000, entity.attributes.person_weekday[i] * 100];
      });
```

**Weekend:**

```yaml
type: custom:apexcharts-card
header:
  title: Street Passerby Probability — Weekend (last 3 weeks)
graph_span: 24h
span:
  start: day
apex_config:
  chart:
    type: bar
  legend:
    show: true
    showForSingleSeries: true
  xaxis:
    type: datetime
    labels:
      format: "HH:mm"
series:
  - entity: sensor.passerby_probability
    name: Weekend
    type: column
    color: "#8E24AA"
    data_generator: |
      const now = new Date();
      const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
      return entity.attributes.labels.map((l, i) => {
        const [hh, mm] = l.split(":").map(Number);
        return [startOfDay + (hh * 60 + mm) * 60000, entity.attributes.person_weekend[i] * 100];
      });
```

If you add a `car` probability entry to `GRAPH_CONFIGS`, duplicate
either card and swap `person_weekday`/`person_weekend` for
`car_weekday`/`car_weekend`.

---

## Adding more graphs later

Add a new dict to `GRAPH_CONFIGS` in the pyscript file with a unique
`sensor` name and the desired `mode` (`"hourly"` or `"probability"`),
zones/labels/etc. No other code changes needed — then add a matching
ApexCharts card pointing at the new sensor's attributes.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Sensor state exists but attributes are empty arrays | `zones`/`required_zones` too strict, wrong zone name, or `min_score` too high |
| Sensor never appears | pyscript not reloaded, or a Python error — check Settings → System → Logs, filter "pyscript" |
| Probability graph looks noisy/spiky | Expected with ~3 weekend/9 weekday samples per bucket at `bucket_min: 5`; widen `bucket_min` to 10–15 or wait for more days of data |
| Hourly graph has too many/too few bars | Number of bars = `(hours_back * 60) / bucket_min`. e.g. 12h at `bucket_min: 15` = 48 bars — fine for ApexCharts, but if you push it much denser than that, either shorten `hours_back` or widen `bucket_min` to keep the chart readable |
| Chart shows nothing in the dashboard, or the x-axis shows raw epoch numbers | This version of apexcharts-card doesn't reliably honor `xaxis: { type: category }` with string labels — it still builds a `datetime` axis internally, which can't parse strings and falls back to showing raw timestamps with no bars. Fix: use `xaxis: { type: datetime }` and feed real millisecond timestamps as the x value — both sensors expose a `timestamps` attribute for exactly this (both card YAMLs already use it) |
| `Configuration error ... value.series[0].data is extraneous` | This apexcharts-card version has no plain `data:` key in its series schema — only `entity` + either recorder history or `data_generator` are valid. Use `data_generator` even for static/test data |
| `NotImplementedError: ... not implemented ast ast_generatorexp` | pyscript's AST interpreter doesn't support Python generator expressions passed directly into a function call (e.g. `sum(x for x in ...)`). The current script avoids this — if you hit it, you're on an older copy, or added custom code using that pattern; wrap it in a list comprehension instead (`sum([x for x in ...])`) |
| Probability card renders but shows no/partial bars, especially "missing" the busiest times of day | Without `graph_span`/`span: { start: day }`, apexcharts-card defaults to a *sliding* 24h window ending at the current real-world moment — but the sensor's `timestamps` are anchored to today's midnight-through-23:55 regardless of what time it actually is. Both probability card YAMLs already set `graph_span: 24h` + `span: { start: day }` to anchor the axis to the start of the calendar day instead — confirm both are present if this happens |
| `series_in_graph[i.seriesIndex].entity` undefined / infinite loading spinner | This apexcharts-card build doesn't properly support `chart.type: heatmap` — its internal graph-index bookkeeping assumes line/bar/area. Avoid heatmap; use the two separate weekday/weekend bar cards instead (already the default in this README) |
| Probability cards worked yesterday, show nothing today (no error) | The sensor's `timestamps` attribute was anchored to "today" as of its last nightly refresh (03:00) — after midnight, that's now "yesterday" until the cron catches up, and the card's `span: { start: day }` always means the browser's actual today. Current card YAML avoids this by computing bucket timestamps client-side from the sensor's `labels` instead of using `timestamps` for these two cards — confirm you're on that version if this happens |
| Frigate API unreachable from pyscript | Check `FRIGATE_URL`, and that HA's pyscript sandbox has network/`requests` access enabled |
| Log warning about blocking calls in the event loop | Shouldn't occur — the script already wraps `requests.get` in `task.executor()` to keep it off pyscript's async loop. If you see one anyway, check you copied the current version of `frigate-detections-stats.py` (the `_get` / `task.executor` helper) |
| `TypeError: pyscript functions can't be called from task.executor` | The helper function passed to `task.executor()` needs the `@pyscript_compile` decorator so pyscript compiles it as a native Python function instead of interpreting it. The current script already has this on `_get` — if you hit this error, check that decorator is present and you're on the latest copy of the script |
| `RuntimeError: Cannot be called from within the event loop` | Caused by calling `hass.states.set(...)` directly instead of pyscript's `state.set(...)`. The current script already uses `state.set(...)` — if you hit this, you're on an older copy |
| Every count is 0 even though Frigate shows events in that window | Some Frigate versions leave the top-level `score`/`top_score` fields `null` and put the real values under `data.score` / `data.top_score`. The current script checks both locations — if you're still seeing all zeros, pull a raw event via `curl .../api/events` and confirm where the score actually lives in your version |
| `NameError: invalid name sensor.x (should be 'pyscript.entity')` | `state.persist()` only works on `pyscript.*` domain entities — it can't persist `sensor.*` (or any other domain) directly, full stop. The current script works around this by mirroring each sensor into a `pyscript.*` cache entity and restoring from that cache on `startup` — if you hit this error, you're on an older copy that tried to persist the sensor entities directly |
| Weekend probability graph only ever shows 0% or 100%, weekday looks smoother | This is expected with a small sample — probability is `hit_days / n_days_sampled`. If `person_weekend_days_sampled` is currently `1` (check the sensor attribute in Developer Tools → States), every bucket can only be 0/1 or 1/1 — no in-between value is mathematically possible yet. Since Frigate retention was only recently extended, historical weekend days cant be backfilled; they accumulate one real calendar day at a time. Weekdays smooth out first simply because there are 5 of them per week vs. 2 weekend days. This self-corrects as more weekends pass within the 21-day window — no fix needed, just time |
| Card was working, now stuck "loading..." after renaming a label in `GRAPH_CONFIGS` | Attribute keys on the sensor come directly from whatever strings are in that entry's `labels` list — renaming a label (e.g. `motorbike` → `motorcycle`) changes the attribute name too. Any card `data_generator` still referencing the old name gets `undefined`, which throws and hangs the card. Update every card that references that label whenever you rename one in `GRAPH_CONFIGS` |
| Probability numbers look wrong/frozen after editing `zones`, `min_score`, `labels`, or `required_zones` in a probability entry | The incremental cache doesn't know a filter changed — it just keeps folding new events into whatever was already cached under the old filter, silently mixing old and new criteria. Run `pyscript.frigate_reset_probability_cache` (Developer Tools → Actions) after changing any filter on a `probability`-mode entry, then trigger `pyscript.frigate_refresh_probability` to force a full reseed |
