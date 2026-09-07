import requests
from datetime import datetime, timedelta

FRIGATE_URL = "http://<frigate-host>:5000"

# --- Refresh schedules ---------------------------------------------------
# Standard 5-field cron syntax: minute hour day month day_of_week
HOURLY_CRON = "*/5 * * * *"        # how often the hourly/short-window graphs refresh
PROBABILITY_CRON = "0 * * * *"     # how often the weekday/weekend probability graph refreshes (hourly — still a 21-day query each run, so more frequent than this gets expensive fast)

# --- Survive HA restarts without re-querying Frigate immediately -------
# state.persist() ONLY works on entities in the pyscript.* domain — it
# rejects sensor.* directly (NameError: invalid name ... should be
# 'pyscript.entity'). So each real sensor's value+attributes are
# mirrored into a matching pyscript.* "cache" entity inside
# compute_hourly()/compute_probability(), that cache entity is what
# gets persisted here, and a startup trigger below copies the restored
# cache back onto the real sensor — giving instant restore without
# hitting Frigate at all.
state.persist("pyscript.frigate_hourly_cache", default_value=0)
state.persist("pyscript.frigate_probability_cache", default_value="unknown")
# Separate from the display cache above: this one holds the raw
# per-day/per-bucket hit data that compute_probability() needs to do
# incremental (delta-fetch) updates instead of a full 21-day rescan
# every run. See compute_probability() for details.
state.persist("pyscript.frigate_probability_daily_cache", default_value="ok")

# --- Graph definitions -------------------------------------------------
# zones:          OR filter — event counts if it touched ANY of these. [] = any zone (no filter).
# required_zones: AND filter — event only counts if it touched ALL of these too. [] = no requirement.
# labels:         each counted/graphed separately.
# sub_labels:     OR filter on sub_label (e.g. recognized plate/face). [] = no filter.
# min_score:      drop events below this confidence.

GRAPH_CONFIGS = [
# NOTE: the restart-persistence cache (pyscript.frigate_hourly_cache /
# pyscript.frigate_probability_cache) is keyed to ONE entry per mode.
# If you add a second "hourly" or "probability" entry below, its
# sensor won't get restored on restart with the current cache setup —
# only the last one computed each cycle will occupy the shared cache
# entity. Fine for the two default entries; flag it if you expand.
    {
        "sensor": "sensor.frigate_hourly_detections",
        "mode": "hourly",
        "hours_back": 12,
        "bucket_min": 5,    # bar width in minutes; omit for hourly (default 60)
        "labels": ["bicycle", "bus", "car", "motorcycle", "person"],
        "zones": [],
        "required_zones": [],
        "sub_labels": [],
        "min_score": 0.7,
    },
    {
        "sensor": "sensor.passerby_probability",
        "mode": "probability",
        "days_back": 21,
        "bucket_min": 5,
        "labels": ["bicycle", "car", "motorcycle", "person"],
        "zones": ["street"],
        "required_zones": [],
        "sub_labels": [],
        "min_score": 0.7,
    },
]

# --- Shared fetch/filter -------------------------------------------------

@pyscript_compile
def _get(params):
    # Plain blocking call — must be run via task.executor() from async
    # pyscript context, never called directly. @pyscript_compile makes
    # this a native Python function (required for task.executor).
    resp = requests.get(f"{FRIGATE_URL}/api/events", params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_events(after_ts, label, zones=None, required_zones=None,
                  sub_labels=None, min_score=0.7):
    params = {"after": int(after_ts), "labels": label}
    if zones:
        params["zones"] = ",".join(zones)  # Frigate's own filter = OR

    events = task.executor(_get, params)  # offload blocking I/O

    seen_ids = set()
    out = []
    for ev in events:
        eid = ev.get("id")
        if eid in seen_ids:          # dedup: one count per finished track
            continue

        data = ev.get("data") or {}
        score = ev.get("score") or ev.get("top_score") or \
            data.get("score") or data.get("top_score") or 0
        if score < min_score:
            continue

        if required_zones:
            ev_zones = set(ev.get("zones") or [])
            if not set(required_zones).issubset(ev_zones):
                continue

        if sub_labels:
            if ev.get("sub_label") not in sub_labels:
                continue

        seen_ids.add(eid)
        out.append(ev)
    return out

# --- Hourly bar graph ------------------------------------------------

def compute_hourly(cfg):
    bucket_min = cfg.get("bucket_min", 60)  # minutes per bar; default = 1 hour
    window_min = cfg["hours_back"] * 60
    n_buckets = window_min // bucket_min
    bucket_seconds = bucket_min * 60

    now = datetime.now()
    start = now - timedelta(minutes=window_min)
    labels = [(start + timedelta(minutes=bucket_min * i)).strftime("%H:%M")
              for i in range(n_buckets)]
    timestamps = [int((start + timedelta(minutes=bucket_min * i)).timestamp() * 1000)
                  for i in range(n_buckets)]

    attrs = {"hours": labels, "timestamps": timestamps,
             "unit_of_measurement": "events"}
    total = 0

    for label in cfg["labels"]:
        counts = [0] * n_buckets
        events = fetch_events(
            start.timestamp(), label,
            zones=cfg["zones"], required_zones=cfg["required_zones"],
            sub_labels=cfg["sub_labels"], min_score=cfg["min_score"],
        )
        for ev in events:
            ts = datetime.fromtimestamp(ev["start_time"])
            idx = int((ts - start).total_seconds() // bucket_seconds)
            if 0 <= idx < n_buckets:
                counts[idx] += 1
        attrs[label] = counts
        total += sum(counts)

    state.set(cfg["sensor"], total, new_attributes=attrs)
    state.set("pyscript.frigate_hourly_cache", total, new_attributes=attrs)

# --- Probability bar graph --------------------------------------------
#
# Incremental design: instead of re-fetching and recomputing the full
# 21-day window every run, this keeps a persisted per-day/per-bucket
# "hit set" (which 5-min buckets had at least one detection, per day,
# per label) in pyscript.frigate_probability_daily_cache. Each run:
#   1. Fetch only events since the last processed timestamp (cheap).
#   2. Fold new hits into today's entry in the per-day cache.
#   3. Drop any day older than days_back from the cache (rolling window).
#   4. Recompute weekday/weekend probabilities from the *retained*
#      per-day cache — pure local math, no Frigate query involved.
#   5. Persist the updated per-day cache + a "last processed" watermark.
# If there's no cache yet, or the watermark is older than the days_back
# window (e.g. after extended downtime), it falls back to a full
# rescan of the window to reseed itself — this should only happen once
# under normal operation.
#
# NOTE: keyed by cfg["sensor"], so multiple probability entries in
# GRAPH_CONFIGS (if you ever add more) don't collide with each other,
# even though — like the display cache — this is one shared cache
# entity across all of them.

DAILY_CACHE_ENTITY = "pyscript.frigate_probability_daily_cache"

def compute_probability(cfg):
    now = datetime.now()
    n_buckets = (24 * 60) // cfg["bucket_min"]
    labels_axis = [f"{(b*cfg['bucket_min'])//60:02d}:{(b*cfg['bucket_min'])%60:02d}"
                   for b in range(n_buckets)]
    # Only time-of-day matters here (data is aggregated across many days),
    # so anchor bucket timestamps to an arbitrary reference day (today) —
    # this just gives the chart real datetime values to plot against.
    ref_midnight = datetime.combine(now.date(), datetime.min.time())
    timestamps = [int((ref_midnight + timedelta(minutes=cfg["bucket_min"] * b)).timestamp() * 1000)
                  for b in range(n_buckets)]

    attrs = {"labels": labels_axis, "timestamps": timestamps}

    sensor_key = cfg["sensor"]
    window_start_ts = (now - timedelta(days=cfg["days_back"])).timestamp()
    cutoff_date = (now - timedelta(days=cfg["days_back"])).date()

    cache_attrs = state.getattr(DAILY_CACHE_ENTITY) or {}
    all_daily_hits = cache_attrs.get("daily_hits", {})
    all_last_ts = cache_attrs.get("last_processed_ts", {})

    sensor_daily_hits = all_daily_hits.get(sensor_key, {})   # {label: {date_str: [bucket, ...]}}
    last_ts = all_last_ts.get(sensor_key)

    # Reseed with a full rescan if there's no watermark yet, or it's
    # older than the window itself (e.g. long HA downtime) — otherwise
    # a delta fetch from the watermark is all that's needed.
    if last_ts is None or last_ts < window_start_ts:
        fetch_start_ts = window_start_ts
        sensor_daily_hits = {}
    else:
        fetch_start_ts = last_ts

    for label in cfg["labels"]:
        label_hits = {d: set(b) for d, b in sensor_daily_hits.get(label, {}).items()}

        events = fetch_events(
            fetch_start_ts, label,
            zones=cfg["zones"], required_zones=cfg["required_zones"],
            sub_labels=cfg["sub_labels"], min_score=cfg["min_score"],
        )
        for ev in events:
            ts = datetime.fromtimestamp(ev["start_time"])
            date_str = ts.strftime("%Y-%m-%d")
            bucket = (ts.hour * 60 + ts.minute) // cfg["bucket_min"]
            label_hits.setdefault(date_str, set()).add(bucket)

        # Expire days that have aged out of the rolling window.
        label_hits = {d: b for d, b in label_hits.items()
                      if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_date}

        weekday_dates = [d for d in label_hits if datetime.strptime(d, "%Y-%m-%d").weekday() < 5]
        weekend_dates = [d for d in label_hits if datetime.strptime(d, "%Y-%m-%d").weekday() >= 5]

        def probs(dates):
            n_days = max(len(dates), 1)
            return [round(sum([1 for d in dates if b in label_hits[d]]) / n_days, 3)
                    for b in range(n_buckets)]

        attrs[f"{label}_weekday"] = probs(weekday_dates)
        attrs[f"{label}_weekend"] = probs(weekend_dates)
        attrs[f"{label}_weekday_days_sampled"] = len(weekday_dates)
        attrs[f"{label}_weekend_days_sampled"] = len(weekend_dates)

        # Store back as sorted lists — sets aren't JSON-safe for state storage.
        sensor_daily_hits[label] = {d: sorted(b) for d, b in label_hits.items()}

    all_daily_hits[sensor_key] = sensor_daily_hits
    all_last_ts[sensor_key] = now.timestamp()
    state.set(DAILY_CACHE_ENTITY, "ok", new_attributes={
        "daily_hits": all_daily_hits,
        "last_processed_ts": all_last_ts,
    })

    state.set(cfg["sensor"], "ok", new_attributes=attrs)
    state.set("pyscript.frigate_probability_cache", "ok", new_attributes=attrs)

# --- Triggers ------------------------------------------------------------

@time_trigger("startup")
def restore_from_cache():
    # Instant restore on HA restart, no Frigate query — copies whatever
    # was last persisted in the pyscript.* cache entities back onto the
    # real sensors. They'll self-correct at the next scheduled/manual
    # refresh if the cached data is stale.
    #
    # If there's no cache yet at all (very first run after installing
    # this script, before anything has ever populated it), fall back to
    # computing it live right now instead of leaving the sensor empty
    # until the next cron tick — this only happens once, not on every
    # restart, since after this run the cache always has something in it.
    try:
        cached_attrs = state.getattr("pyscript.frigate_hourly_cache")
        if cached_attrs:
            cached_value = state.get("pyscript.frigate_hourly_cache")
            state.set("sensor.frigate_hourly_detections", cached_value, new_attributes=cached_attrs)
        else:
            for cfg in GRAPH_CONFIGS:
                if cfg["mode"] == "hourly":
                    compute_hourly(cfg)
    except Exception:
        pass

    try:
        cached_attrs = state.getattr("pyscript.frigate_probability_cache")
        if cached_attrs:
            cached_value = state.get("pyscript.frigate_probability_cache")
            state.set("sensor.passerby_probability", cached_value, new_attributes=cached_attrs)
        else:
            for cfg in GRAPH_CONFIGS:
                if cfg["mode"] == "probability":
                    compute_probability(cfg)
    except Exception:
        pass

@service
@time_trigger(f"cron({HOURLY_CRON})")
def frigate_refresh_hourly():
    for cfg in GRAPH_CONFIGS:
        if cfg["mode"] == "hourly":
            compute_hourly(cfg)

@service
@time_trigger(f"cron({PROBABILITY_CRON})")
def frigate_refresh_probability():
    for cfg in GRAPH_CONFIGS:
        if cfg["mode"] == "probability":
            compute_probability(cfg)

@service
def frigate_reset_probability_cache():
    # Wipes the incremental per-day hit cache, forcing the next
    # frigate_refresh_probability run to do a full rescan and reseed
    # from scratch. Call this after changing zones/min_score/labels/
    # required_zones on a probability entry — the incremental design
    # otherwise just keeps folding new events into data gathered under
    # the OLD filter settings, silently mixing old and new criteria.
    state.set(DAILY_CACHE_ENTITY, "ok", new_attributes={})
