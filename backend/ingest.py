#!/usr/bin/env python3
"""
ingest.py — Denmark Surf Forecast · Phase 0 data ingestion
==========================================================

Fetches live marine + weather data from the DMI open data APIs (no API key
required at the time of writing) and writes a single consolidated
``frontend/public/data/forecast.json`` consumed by the static frontend.

Data sources
------------
1. Forecast EDR API (https://dmigw.govcloud.dk/v1/forecastedr)

   * WAM wave model — collection ``wam_dw`` (Danish Waters) with per-spot
     fallback to ``wam_nsb`` (North Sea–Baltic).
   * HARMONIE-DINI weather — collection ``harmonie_dini_sf``.

   The EDR ``/position`` endpoint is currently heavily throttled server-side
   (HTTP 429 "Server is busy. Please try again later."). Every request
   therefore uses patient exponential backoff: up to 8 attempts per call with
   waits of roughly 10 → 90 s between attempts.

2. OceanObs tide predictions
   (https://opendataapi.dmi.dk/v2/oceanObs/collections/tidewater/items)
   10-minute predicted water level series plus minimum/maximum (low/high
   tide) entries, in cm relative to DVR90.

Resilience strategy
-------------------
The script is incremental and resumable:

* every successful raw fetch is cached under ``backend/cache/``;
* re-running within the freshness window reuses the cache instead of
  hammering the API (counts as "ok" — it is recent live data);
* if a source cannot be refreshed but an older cache exists, the cached
  values are reused and flagged ``"stale"``;
* if no cache exists but a previous ``forecast.json`` has usable future
  values, those are reused (also ``"stale"``);
* only when nothing exists at all (first run ever + source down) do we emit
  clearly marked ``"sample": true`` placeholder data so the page renders.

A global time budget (``--budget-seconds``, default 240) keeps every single
run bounded; simply run the script repeatedly until all sources report "ok".

Usage
-----
    python backend/ingest.py                    # bounded ~4 min run
    python backend/ingest.py --budget-seconds 600
    python backend/ingest.py --force            # ignore cache freshness
    python backend/ingest.py --from-cache       # offline rebuild from backend/cache
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "frontend" / "public" / "data" / "forecast.json"
CACHE_DIR = ROOT / "backend" / "cache"

EDR_BASE = "https://dmigw.govcloud.dk/v1/forecastedr"
OCEAN_BASE = "https://opendataapi.dmi.dk/v2/oceanObs"

WAM_COLLECTIONS = ["wam_dw", "wam_nsb"]  # default per-spot fallback order
HARMONIE_COLLECTION = "harmonie_dini_sf"

# Per-spot WAM preference. The open North-Sea coastline (Klitmøller, ~8.5°E)
# sits outside the wam_dw "Danish Waters" high-res domain — verified
# empirically: wam_dw /position returns features with all parameters null
# there (0/121 numeric steps) instead of an error, which previously masked
# the fallback. Those spots therefore query wam_nsb (North Sea–Baltic) first
# and keep wam_dw as a last-ditch fallback.
WAM_COLLECTIONS_BY_SPOT = {
    "hornbaek-ostre": ["wam_nsb", "wam_dw"],  # Kattegat is inside the NSB domain
    "klitmoller-bugten": ["wam_nsb", "wam_dw"],
    "bunkers-klitmoller": ["wam_nsb", "wam_dw"],
    # Open North-Sea coastline at 8.1–8.6°E — same out-of-wam_dw-domain
    # situation as Klitmøller (verified all-null there), so NSB first.
    "middles-hanstholm": ["wam_nsb", "wam_dw"],
    "vorupor-bugten": ["wam_nsb", "wam_dw"],
    "agger": ["wam_nsb", "wam_dw"],
    "thorsminde": ["wam_nsb", "wam_dw"],
    "hvide-sande": ["wam_nsb", "wam_dw"],
}

WAM_PARAMS = [
    "significant-wave-height",
    "significant-totalswell-height",
    "mean-totalswell-period",
    "mean-totalswell-dir",
    "significant-windwave-height",
    "mean-wave-dir",
    # WAM marine 10 m wind — used to extend the wind forecast beyond the
    # HARMONIE horizon (~2.5 days) out to the full WAM range (5 days)
    "wind-speed",
    "wind-dir",
]
HARMONIE_PARAMS = [
    "wind-speed-10m",
    "wind-dir-10m",
    "gust-wind-speed-10m",
    "temperature-2m",  # Kelvin — converted to °C on composition
]

FORECAST_DAYS = 5
HARMONIE_HOURS = 60  # HARMONIE-DINI only reaches ~2.5 days ahead
BACKOFF_WAITS_S = [4, 8, 12, 20, 30, 45, 60]  # ~8 attempts per request
REQUEST_TIMEOUT_S = 30
POLITE_PAUSE_S = 1.2  # serialize calls: >= 1 s between requests

# DMI publishes new model runs every 6 h (00/06/12/18 UTC), so a 6 h cache
# freshness means at most one refetch per model cycle — and keeps the whole
# time budget for spots that have no data at all when playing catch-up.
CACHE_FRESH_WAM_S = 6 * 3600
CACHE_FRESH_HARMONIE_S = 6 * 3600
CACHE_FRESH_TIDES_S = 6 * 3600

SPOTS = [
    {
        "id": "hornbaek-ostre",
        "name": "Hornbæk",
        "lat": 56.0950,             # extraction point from user's Hornbæk.kmz (offshore)
        "lon": 12.4639,
        "facing_deg": 25,          # faces NNE
        "region": "Nordsjælland · Kattegat",
        "swell_window": (350, 70),  # optimal swell direction N–ENE
        "offshore_sector": (160, 250),  # S–WSW
        "tide_station": "30017",    # Hornbæk
        "tide_lat": 56.0934,
        "tide_lon": 12.4571,
        # local knowledge: harbour shelters everything except hard NW — the
        # v1 rating covers this via the open-water-window sheltered test
    },
    {
        "id": "klitmoller-bugten",
        "name": "Klitmøller Bugten",
        "lat": 57.0460,             # extraction point from user's Klitmøller Bugten.kmz
        "lon": 8.4903,
        "facing_deg": 285,          # faces WNW
        "region": "Vestkysten · North Sea",
        "swell_window": (255, 345),  # W–N
        "offshore_sector": (80, 160),  # E–SE
        "tide_station": "21009",    # Hanstholm (~8 km north)
        "tide_lat": 57.12,
        "tide_lon": 8.5955,
        # local knowledge: wind from >= 270 deg is unsheltered — covered by
        # the v1 rating's open-water-window sheltered test
    },
    {
        "id": "bunkers-klitmoller",
        "name": "Bunkers (Klitmøller)",
        "lat": 57.0395,             # extraction point from user's Bunkers (Klitmøller).kmz
        "lon": 8.4637,
        "facing_deg": 275,          # faces W
        "region": "Vestkysten · North Sea",
        "swell_window": (245, 335),  # W–NW
        "offshore_sector": (70, 150),  # E
        "tide_station": "21009",
        "tide_lat": 57.12,
        "tide_lon": 8.5955,
        # local knowledge: only offshore wind (70–150°) is truly favorable —
        # covered by the v1 rating (its open-water window is wide, so most
        # other directions fail the sheltered test)
    },
    # ------------------------------------------------------------------
    # Additional spots (extraction points from user's KMZ files).
    # No local wind rules yet — to be calibrated with the spot owner.
    # ------------------------------------------------------------------
    {
        "id": "middles-hanstholm",
        "name": "Middles (Hanstholm)",
        "lat": 57.1258,
        "lon": 8.6337,
        "facing_deg": 290,
        "region": "Vestkysten · North Sea",
        "swell_window": (255, 345),
        "offshore_sector": (80, 160),
        "tide_station": "21009",    # Hanstholm (2 km)
        "tide_lat": 57.12,
        "tide_lon": 8.5955,
    },
    {
        "id": "vorupor-bugten",
        "name": "Vorupør Bugten",
        "lat": 56.9615,
        "lon": 8.3694,
        "facing_deg": 285,
        "region": "Vestkysten · North Sea",
        "swell_window": (255, 345),
        "offshore_sector": (80, 160),
        "tide_station": "21009",    # Hanstholm (~22 km, open coast)
        "tide_lat": 57.12,
        "tide_lon": 8.5955,
    },
    {
        "id": "agger",
        "name": "Agger",
        "lat": 56.7274,
        "lon": 8.2150,
        "facing_deg": 275,
        "region": "Vestkysten · North Sea",
        "swell_window": (250, 340),
        "offshore_sector": (80, 160),
        "tide_station": "24006",    # Thyborøn kyst (2 km)
        "tide_lat": 56.7077,
        "tide_lon": 8.2088,
    },
    {
        "id": "thorsminde",
        "name": "Thorsminde",
        "lat": 56.3685,
        "lon": 8.1134,
        "facing_deg": 270,
        "region": "Vestkysten · North Sea",
        "swell_window": (250, 340),
        "offshore_sector": (80, 160),
        "tide_station": "24122",    # Thorsminde kyst (0.5 km)
        "tide_lat": 56.3726,
        "tide_lon": 8.1136,
    },
    {
        "id": "hvide-sande",
        "name": "Hvide Sande",
        "lat": 55.9934,
        "lon": 8.1132,
        "facing_deg": 270,
        "region": "Vestkysten · North Sea",
        "swell_window": (250, 340),
        "offshore_sector": (80, 160),
        "tide_station": "24342",    # Hvide Sande kyst (0.4 km)
        "tide_lat": 55.9967,
        "tide_lon": 8.1098,
    },
    {
        "id": "lokken-molen",
        "name": "Løkken (Molen)",
        "lat": 57.3736,
        "lon": 9.7048,
        "facing_deg": 340,
        "region": "Nordjylland · Skagerrak",
        "swell_window": (300, 60),
        "offshore_sector": (150, 220),
        "tide_station": "20047",    # Hirtshals (~29 km)
        "tide_lat": 57.5951,
        "tide_lon": 9.9625,
    },
    {
        "id": "raageleje",
        "name": "Rågeleje",
        "lat": 56.1003,
        "lon": 12.1597,
        "facing_deg": 0,
        "region": "Nordsjælland · Kattegat",
        "swell_window": (330, 60),
        "offshore_sector": (140, 230),
        "tide_station": "30106",    # Frederiksværk (~18 km)
        "tide_lat": 55.9653,
        "tide_lon": 12.0005,
    },
    {
        "id": "nakkehoved",
        "name": "Nakkehoved",
        "lat": 56.1212,
        "lon": 12.3478,
        "facing_deg": 20,
        "region": "Nordsjælland · Kattegat",
        "swell_window": (340, 70),
        "offshore_sector": (150, 240),
        "tide_station": "30017",    # Hornbæk (7 km)
        "tide_lat": 56.0934,
        "tide_lon": 12.4571,
    },
    {
        "id": "havstokken",
        "name": "Havstokken",
        "lat": 56.1146,
        "lon": 12.2034,
        "facing_deg": 5,
        "region": "Nordsjælland · Kattegat",
        "swell_window": (330, 60),
        "offshore_sector": (140, 230),
        "tide_station": "30017",    # Hornbæk (~16 km)
        "tide_lat": 56.0934,
        "tide_lon": 12.4571,
    },
    {
        "id": "klint",
        "name": "Klint",
        "lat": 55.9574,
        "lon": 11.6006,
        "facing_deg": 10,
        "region": "NV Sjælland · Kattegat",
        "swell_window": (330, 60),
        "offshore_sector": (140, 240),
        "tide_station": "29014",    # Nykøbing Sjælland Havn (7 km)
        "tide_lat": 55.9134,
        "tide_lon": 11.6752,
    },
    {
        "id": "rodvig-havn",
        "name": "Rødvig Havn",
        "lat": 55.2512,
        "lon": 12.3702,
        "facing_deg": 110,
        "region": "Stevns · Fakse Bugt",
        "swell_window": (60, 160),
        "offshore_sector": (240, 330),
        "tide_station": "31063",    # Rødvig (0.4 km)
        "tide_lat": 55.2543,
        "tide_lon": 12.3744,
    },
]

USER_AGENT = "dk-surf-forecast-phase0/0.1 (+local prototype)"


class SourceUnavailable(Exception):
    """Raised when a remote source could not be fetched within budget."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    print(f"[{stamp}] {msg}", flush=True)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def step_key(iso_str: str) -> str:
    """Normalize any ISO-8601 UTC timestamp to minute precision for joining."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def key_to_iso(key: str) -> str:
    dt = datetime.strptime(key, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    return iso_z(dt)


def parse_iso(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(timezone.utc)


def in_sector(deg: float, start: float, end: float) -> bool:
    """True if compass direction ``deg`` lies inside sector start→end going
    clockwise. Handles sectors wrapping around 0° (e.g. 350°–70°)."""
    deg, start, end = deg % 360.0, start % 360.0, end % 360.0
    if start <= end:
        return start <= deg <= end
    return deg >= start or deg <= end


def ang_diff(a: float, b: float) -> float:
    """Smallest absolute angular difference between two compass directions."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def rnd(v, nd=2):
    return round(float(v), nd) if isinstance(v, (int, float)) else None


def rnd_dir(v):
    return int(round(float(v))) % 360 if isinstance(v, (int, float)) else None


# ---------------------------------------------------------------------------
# Surf rating v1 — region-calibrated, wind-gated (pure function, no I/O)
# ---------------------------------------------------------------------------
#
# Design (per the spot owner):
#   * Wave height, wave PERIOD (peak Tp) and WIND drive surf quality.
#   * "Epic" is LOCAL to the spot's wave climate: 1.2 m @ 6.5 s can be epic
#     in the Kattegat, while the exposed North Sea coast wants 1.8 m @ 8 s.
#     Each region therefore has its own piecewise-linear Hs/Tp anchors.
#   * Epic (>= 4.5) is only possible with FAVORABLE wind and ridable waves:
#       favorable := ws < 3 m/s, OR wind from the offshore sector, OR wind
#       NOT arriving over the spot's open-water window (= sheltered by land
#       or harbour). This one sheltered test reproduces all the local rules:
#       Hornbaek NW-exposure, Klitmoller >=270 deg, Bunkers offshore-only.
#   * Otherwise caps apply: moderate wind -> max 4.0, poor -> 3.0, bad -> 2.5,
#     and waves below the region's ridable gate -> max 3.0 (Fair).

RATING_LABELS = [
    (1.0, "Poor"),
    (2.0, "Fair−"),
    (3.0, "Fair"),
    (4.0, "Good"),
    (5.0, "Epic"),
]


def rating_label(score: float) -> str:
    for threshold, label in RATING_LABELS:
        if score <= threshold:
            return label
    return "Epic"


RATING_REGIONS = {
    # Exposed Skagerrak / North Sea coast — powerful, consistent swell.
    "northsea": {
        "hs": [(0.0, 0), (0.4, 1), (0.7, 2), (1.0, 3), (1.4, 4), (1.8, 5)],
        "tp": [(0.0, 0), (4.0, 1), (5.0, 2), (6.0, 3), (7.0, 4), (8.0, 5)],
        "gate": (1.0, 6.0),      # ridable minimum for epic: (Hs m, Tp s)
    },
    # Kattegat — fetch-limited, shorter period windswell.
    "kattegat": {
        "hs": [(0.0, 0), (0.3, 1), (0.5, 2), (0.7, 3), (1.0, 4), (1.2, 5)],
        "tp": [(0.0, 0), (3.5, 1), (4.5, 2), (5.5, 3), (6.0, 4), (6.5, 5)],
        "gate": (0.7, 5.5),
    },
    # Western Baltic approach (Rodvig) — very fetch-limited.
    "baltic": {
        "hs": [(0.0, 0), (0.25, 1), (0.4, 2), (0.55, 3), (0.75, 4), (0.9, 5)],
        "tp": [(0.0, 0), (3.0, 1), (4.0, 2), (5.0, 3), (5.5, 4), (6.0, 5)],
        "gate": (0.55, 5.0),
    },
}

# How the base wave score mixes height and period, and how swell direction
# outside the (widened) optimal window discounts it.
HS_WEIGHT = 0.65
DIR_FACTOR_EDGE = 0.7   # within 45 deg beyond the widened window
DIR_FACTOR_OUT = 0.4    # further out


def rating_region(spot) -> str:
    """Region key from the spot's human region string (explicit
    ``rating_region`` in the spot config wins when present)."""
    if spot.get("rating_region"):
        return spot["rating_region"]
    r = spot.get("region", "")
    if "North Sea" in r or "Skagerrak" in r:
        return "northsea"
    if "Kattegat" in r:
        return "kattegat"
    return "baltic"


def _anchor_score(anchors, v):
    """Piecewise-linear lookup of (value -> score 0..5) anchor tables."""
    if not isinstance(v, (int, float)):
        return None
    if v <= anchors[0][0]:
        return anchors[0][1]
    for k in range(1, len(anchors)):
        if v <= anchors[k][0]:
            x0, y0 = anchors[k - 1]
            x1, y1 = anchors[k]
            return y0 + (y1 - y0) * (v - x0) / (x1 - x0)
    return anchors[-1][1]


def wind_class(wind_ms, wind_dir_deg, spot):
    """
    Classify the wind for one hour at one spot.
    Returns (name, factor, cap) with name in
    favorable | moderate | poor | bad | unknown.

    favorable: ws < 3 m/s (glassy), OR offshore sector, OR sheltered —
      i.e. the wind does NOT arrive over the spot's open-water swell window
      (widened by +/-45 deg), so land/harbour blocks it.
    bad:       onshore (within +/-45 deg of the direction the spot faces)
      and ws >= 8 m/s, OR ws >= 12 m/s from any direction.
    poor:      onshore and ws >= 5 m/s, OR ws >= 8 m/s cross-shore.
    moderate:  everything else with data; unknown: missing data (neutral).
    """
    if not (isinstance(wind_ms, (int, float)) and isinstance(wind_dir_deg, (int, float))):
        return "unknown", 1.0, 5.0
    if wind_ms < 3.0:
        return "favorable", 1.0, 5.0
    if in_sector(wind_dir_deg, spot["offshore_sector"][0], spot["offshore_sector"][1]):
        return "favorable", 1.0, 5.0
    w0 = (spot["swell_window"][0] - 45.0) % 360.0
    w1 = (spot["swell_window"][1] + 45.0) % 360.0
    over_water = in_sector(wind_dir_deg, w0, w1)
    if not over_water:
        return "favorable", 1.0, 5.0   # sheltered by land / harbour
    onshore = ang_diff(wind_dir_deg, spot["facing_deg"]) <= 45.0
    if (onshore and wind_ms >= 8.0) or wind_ms >= 12.0:
        return "bad", 0.35, 2.5
    if (onshore and wind_ms >= 5.0) or wind_ms >= 8.0:
        return "poor", 0.6, 3.0
    return "moderate", 0.85, 4.0


def surf_rating(*, hs, tp, swell_dir_deg, wind_ms, wind_dir_deg, spot):
    """
    Surf rating v1. Returns (score, label, capped, note, wind_class_name).

    Inputs (any may be None when a source is missing):
      hs            wave height in m (caller passes total-swell height,
                    falling back to total significant height)
      tp            peak wave period in s (falls back to mean period)
      swell_dir_deg swell / mean wave direction, degrees FROM
      wind_ms / wind_dir_deg   10 m wind, m/s and degrees FROM
      spot          spot metadata (facing_deg, swell_window, offshore_sector,
                    region / rating_region)

    Pipeline: region-calibrated base = 0.65 * Hs anchor + 0.35 * Tp anchor;
    swell-direction discount; wind factor; then gates — never epic with
    unfavorable wind (cap by class) or below-ridable waves (cap 3.0).
    Score is clamped to [0, 5] and rounded to the nearest 0.5.
    """
    reg = RATING_REGIONS[rating_region(spot)]
    hs_s = _anchor_score(reg["hs"], hs)
    tp_s = _anchor_score(reg["tp"], tp)
    if hs_s is None and tp_s is None:
        base = 0.0
    elif hs_s is None:
        base = tp_s
    elif tp_s is None:
        base = hs_s
    else:
        base = HS_WEIGHT * hs_s + (1.0 - HS_WEIGHT) * tp_s

    if isinstance(swell_dir_deg, (int, float)):
        w0 = (spot["swell_window"][0] - 45.0) % 360.0
        w1 = (spot["swell_window"][1] + 45.0) % 360.0
        if not in_sector(swell_dir_deg, w0, w1):
            d = min(ang_diff(swell_dir_deg, w0), ang_diff(swell_dir_deg, w1))
            base *= DIR_FACTOR_EDGE if d <= 45.0 else DIR_FACTOR_OUT

    wname, wfactor, wcap = wind_class(wind_ms, wind_dir_deg, spot)
    raw = base * wfactor
    score = raw
    notes = []

    if wname not in ("favorable", "unknown") and score > wcap:
        score = wcap
        notes.append(f"wind {wname} ({wind_ms:.0f} m/s, {int(round(wind_dir_deg)) % 360}°)")

    gh, gt = reg["gate"]
    ridable = (isinstance(hs, (int, float)) and isinstance(tp, (int, float))
               and hs > gh and tp > gt)
    if score > 3.0 and not ridable:
        score = 3.0
        notes.append(f"waves below {gh} m / {gt} s ridable minimum for this coast")

    capped = score < raw - 1e-9
    score = min(5.0, max(0.0, score))
    score = round(score * 2.0) / 2.0
    return score, rating_label(score), capped, "; ".join(notes) or None, wname


# ---------------------------------------------------------------------------
# HTTP with patient backoff (the EDR endpoint 429s a lot)
# ---------------------------------------------------------------------------

def http_get_json(url: str, what: str, deadline: float):
    waits = BACKOFF_WAITS_S
    last_err = "no attempt made"
    for attempt in range(1, len(waits) + 2):
        if deadline - time.time() < 15:
            raise SourceUnavailable(f"{what}: time budget exhausted ({last_err})")
        time.sleep(POLITE_PAUSE_S)  # serialize requests, >= 1 s apart
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:160]
            except Exception:
                pass
            last_err = f"HTTP {e.code} {body.strip()}"
            retryable = e.code == 429 or 500 <= e.code < 600
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
            retryable = True
        except json.JSONDecodeError as e:
            last_err = f"bad JSON: {e}"
            retryable = True
        if not retryable or attempt > len(waits):
            raise SourceUnavailable(f"{what}: {last_err} (attempt {attempt})")
        wait = min(waits[attempt - 1], max(5.0, deadline - time.time() - 10.0))
        log(f"  {what}: {last_err} — attempt {attempt}/{len(waits) + 1}, retry in {int(wait)} s")
        time.sleep(wait)
    raise SourceUnavailable(f"{what}: {last_err}")


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_edr_series(collection, spot, params, t_from, t_to, deadline,
                     required_param=None):
    """One EDR /position query → (rows, meta). rows: [{step, param, ...}].

    If ``required_param`` is given, at least ~25% of timesteps must carry a
    numeric value for it — the API sometimes returns features whose parameter
    values are all null (point outside the model domain or a broken model
    run), which must trigger the collection fallback just like a 429 storm.
    """
    coords = f"POINT({spot['lon']:.4f}%20{spot['lat']:.4f})"
    url = (
        f"{EDR_BASE}/collections/{collection}/position"
        f"?coords={coords}"
        f"&parameter-name={','.join(params)}"
        f"&datetime={iso_z(t_from)}/{iso_z(t_to)}"
        f"&crs=crs84&f=GeoJSON"
    )
    data = http_get_json(url, f"EDR {collection}/{spot['id']}", deadline)
    features = data.get("features", []) if isinstance(data, dict) else []
    rows = []
    for f in features:
        p = f.get("properties", {})
        step = p.get("step")
        if not step:
            continue
        row = {"step": step_key(step)}
        for name in params:
            row[name] = p.get(name)
        rows.append(row)
    rows.sort(key=lambda r: r["step"])
    if required_param and rows:
        good = sum(1 for r in rows if isinstance(r.get(required_param), (int, float)))
        if good < max(1, len(rows) // 4):
            raise SourceUnavailable(
                f"{collection}: '{required_param}' numeric in only {good}/{len(rows)} "
                f"steps (outside model domain or broken run)"
            )
    meta = {
        "collection": collection,
        "fetched_at": iso_z(datetime.now(timezone.utc)),
        "steps": len(rows),
        "first_step": key_to_iso(rows[0]["step"]) if rows else None,
        "last_step": key_to_iso(rows[-1]["step"]) if rows else None,
    }
    return rows, meta


def fetch_tides(station_id, t_from, t_to, deadline):
    """OceanObs tidewater items → (tides, high_low, meta). Follows pagination."""
    url = (
        f"{OCEAN_BASE}/collections/tidewater/items"
        f"?stationId={station_id}"
        f"&datetime={iso_z(t_from)}/{iso_z(t_to)}"
        f"&limit=10000"
    )
    tides, high_low = [], []
    pages = 0
    while url and pages < 5:
        data = http_get_json(url, f"tides/{station_id}", deadline)
        pages += 1
        for f in data.get("features", []):
            p = f.get("properties", {})
            t, v, kind = p.get("predictionTime"), p.get("value"), p.get("predictionType")
            if t is None or not isinstance(v, (int, float)):
                continue
            if kind == "10minutes":
                tides.append({"time": key_to_iso(step_key(t)), "level_cm": round(float(v), 1)})
            elif kind == "maximum":
                high_low.append({"time": key_to_iso(step_key(t)), "type": "high", "level_cm": round(float(v), 1)})
            elif kind == "minimum":
                high_low.append({"time": key_to_iso(step_key(t)), "type": "low", "level_cm": round(float(v), 1)})
        url = None
        for link in data.get("links", []):
            if link.get("rel") == "next" and link.get("href"):
                url = link["href"]
    tides.sort(key=lambda r: r["time"])
    high_low.sort(key=lambda r: r["time"])
    meta = {
        "station": station_id,
        "fetched_at": iso_z(datetime.now(timezone.utc)),
        "points": len(tides),
        "extrema": len(high_low),
    }
    return tides, high_low, meta


# ---------------------------------------------------------------------------
# Cache + previous-output reuse
# ---------------------------------------------------------------------------

def read_cache(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_cache(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def cache_age_s(entry) -> float:
    try:
        return (datetime.now(timezone.utc) - parse_iso(entry["meta"]["fetched_at"])).total_seconds()
    except Exception:
        return float("inf")


def cache_covers(entry, until: datetime) -> bool:
    """True if the cached payload (EDR row list or tides dict) extends at
    least to ``until``."""
    try:
        payload = entry.get("payload")
        rows = payload.get("tides") if isinstance(payload, dict) else payload
        if not rows:
            return False
        ts = rows[-1].get("step") or rows[-1].get("time")
        return parse_iso(ts) >= until
    except Exception:
        return False


def get_source(cache_name, fetch_fn, fresh_s, cover_until, deadline, force, label,
               offline=False):
    """
    Generic fresh-or-stale wrapper.
    Returns (payload, status, meta) where status ∈ ok|stale|missing.
    With ``offline=True`` (``--from-cache``) the network is never touched:
    any cache that still covers the window counts as "ok".
    """
    cache_path = CACHE_DIR / f"{cache_name}.json"
    cached = read_cache(cache_path)
    fresh_ok = not force and cached and cache_age_s(cached) < fresh_s
    offline_ok = offline and cached
    if (fresh_ok or offline_ok) and cache_covers(cached, cover_until):
        mode = "offline cache" if offline else "fresh cache"
        log(f"  {label}: {mode} (age {int(cache_age_s(cached))} s) — reusing live data")
        return cached["payload"], "ok", cached["meta"]
    if offline:
        log(f"  {label}: offline mode, no usable cache — skipping network fetch")
    elif deadline - time.time() > 20:
        try:
            payload, meta = fetch_fn(deadline)
            write_cache(cache_path, {"meta": meta, "payload": payload})
            log(f"  {label}: fetched live ({meta})")
            return payload, "ok", meta
        except SourceUnavailable as e:
            log(f"  {label}: unavailable — {e}")
    else:
        log(f"  {label}: skipped live fetch (no time budget left)")
    if cached and cache_covers(cached, cover_until - timedelta(hours=36)):
        log(f"  {label}: reusing STALE cache (age {int(cache_age_s(cached) / 60)} min)")
        return cached["payload"], "stale", cached.get("meta")
    return None, "missing", None


def load_previous_spot(spot_id):
    try:
        prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    for s in prev.get("spots", []):
        if s.get("id") == spot_id:
            return s
    return None


def future_entries(entries, t0, key="time"):
    cutoff = iso_z(t0 - timedelta(hours=1))
    return [e for e in (entries or []) if isinstance(e.get(key), str) and e[key] >= cutoff]


# ---------------------------------------------------------------------------
# Sample (clearly-marked placeholder) data — absolute last resort
# ---------------------------------------------------------------------------

def window_center(window):
    w0, w1 = window
    return (w0 + ((w1 - w0) % 360.0) / 2.0) % 360.0


def sample_hours(spot, t0, hours=FORECAST_DAYS * 24):
    """Deterministic synthetic series so the UI renders on a cold start with
    all sources down. Always flagged via status "sample" upstream."""
    seed = sum(ord(c) for c in spot["id"]) % 100
    mid = window_center(spot["swell_window"])
    rows = []
    for i in range(hours):
        key = (t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%MZ")
        swell_h = round(max(0.05, 0.55 + 0.30 * math.sin(i / 9 + seed) + 0.12 * math.sin(i / 3.7)), 2)
        period = round(5.5 + 2.0 * math.sin(i / 14 + 1 + seed), 1)
        sdir = (mid + 25 * math.sin(i / 11 + seed)) % 360.0
        wind = round(6.0 + 2.5 * math.sin(i / 7 + 2 + seed), 1)
        wdir = (spot["facing_deg"] + 100 + 45 * math.sin(i / 13 + seed)) % 360.0
        gust = round(wind * 1.45, 1)
        temp = round(13.0 + 2.0 * math.sin(i / 24 + seed), 1)
        hs = round(swell_h + 0.25 * max(0.0, math.sin(i / 5)), 2)
        score, label, capped, note, _wname = surf_rating(
            hs=swell_h, tp=period, swell_dir_deg=sdir,
            wind_ms=wind, wind_dir_deg=wdir, spot=spot,
        )
        rows.append({
            "time": key_to_iso(key), "hs": hs, "swell_h": swell_h,
            "swell_period": period, "swell_dir": int(round(sdir)),
            "windwave_h": round(swell_h * 0.6, 2), "wind_ms": wind,
            "wind_dir": int(round(wdir)), "gust_ms": gust, "air_temp_c": temp,
            "rating": score, "rating_label": label,
            "rating_capped": capped, "rating_note": note,
        })
    return rows


def sample_tides(t0, days=FORECAST_DAYS):
    """Semidiurnal sine placeholder tide (±30 cm around DVR90)."""
    tides, high_low = [], []
    period_h = 12.42
    n = days * 24 * 6
    for i in range(n):
        t = t0 + timedelta(minutes=10 * i)
        level = 30.0 * math.sin(2 * math.pi * (i / 6.0) / period_h)
        tides.append({"time": iso_z(t), "level_cm": round(level, 1)})
    for k in range(int(days * 24 / (period_h / 2)) + 1):
        t = t0 + timedelta(hours=period_h / 4 + k * period_h / 2)
        high = (k % 2 == 0)
        high_low.append({
            "time": iso_z(t), "type": "high" if high else "low",
            "level_cm": 30.0 if high else -30.0,
        })
    return tides, high_low


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def compose_hours(spot, wam_rows, harm_rows, prev_wind_by_time):
    """Join WAM + HARMONIE rows by timestamp; compute rating per hour."""
    harm_by_key = {r["step"]: r for r in (harm_rows or [])}
    out = []
    for r in wam_rows:
        h = harm_by_key.get(r["step"])
        wind_ms = wind_dir = gust = temp = None
        wind_src = None
        if h is not None:
            wind_ms = h.get("wind-speed-10m")
            wind_dir = h.get("wind-dir-10m")
            gust = h.get("gust-wind-speed-10m")
            tk = h.get("temperature-2m")
            if isinstance(tk, (int, float)):
                temp = tk - 273.15  # Kelvin → °C
            wind_src = "harmonie"
        else:
            # beyond the HARMONIE horizon: prefer the WAM model's own marine
            # 10 m wind from the CURRENT fetch (no gust/temperature there)
            wv, wd = r.get("wind-speed"), r.get("wind-dir")
            if isinstance(wv, (int, float)):
                wind_ms = wv
                if isinstance(wd, (int, float)):
                    wind_dir = wd
                wind_src = "wam"
            else:
                # last resort: reused values from a previous run (keep their
                # original source label when known)
                p = (prev_wind_by_time or {}).get(r["step"])
                if p is not None and isinstance(p.get("wind_ms"), (int, float)):
                    wind_ms, wind_dir = p.get("wind_ms"), p.get("wind_dir")
                    gust, temp = p.get("gust_ms"), p.get("air_temp_c")
                    wind_src = p.get("wind_src") or "harmonie"
        swell_h = r.get("significant-totalswell-height")
        hs = r.get("significant-wave-height")
        base_h = swell_h if isinstance(swell_h, (int, float)) else hs
        period = r.get("mean-totalswell-period")
        # Tp (peak period) drives the rating; mean period is the fallback
        tp = r.get("peak-wave-period")
        if not isinstance(tp, (int, float)):
            tp = period
        sdir = r.get("mean-totalswell-dir")
        if not isinstance(sdir, (int, float)):
            sdir = r.get("mean-wave-dir")
        score, label, capped, note, wname = surf_rating(
            hs=base_h, tp=tp, swell_dir_deg=sdir,
            wind_ms=wind_ms, wind_dir_deg=wind_dir, spot=spot,
        )
        out.append({
            "time": key_to_iso(r["step"]),
            "hs": rnd(hs, 2),
            "swell_h": rnd(swell_h, 2),
            "swell_period": rnd(period, 1),
            "peak_period": rnd(tp, 1),
            "swell_dir": rnd_dir(sdir),
            "windwave_h": rnd(r.get("significant-windwave-height"), 2),
            "wind_ms": rnd(wind_ms, 1),
            "wind_dir": rnd_dir(wind_dir),
            "wind_src": wind_src,  # "harmonie" (hi-res, incl. gusts) | "wam" (extended) | null
            "wind_class": wname,   # favorable | moderate | poor | bad | unknown
            "gust_ms": rnd(gust, 1),
            "air_temp_c": rnd(temp, 1),
            "rating": score,
            "rating_label": label,
            "rating_capped": capped,
            "rating_note": note,
        })
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="DK Surf Forecast ingestion (DMI open data)")
    ap.add_argument("--budget-seconds", type=int, default=240,
                    help="global wall-clock budget for network fetching (default 240)")
    ap.add_argument("--force", action="store_true", help="ignore cache freshness, refetch all")
    ap.add_argument("--from-cache", action="store_true",
                    help="offline mode: rebuild forecast.json from backend/cache only, "
                         "no network calls (ratings/flags recomputed)")
    args = ap.parse_args()

    started = time.time()
    deadline = started + args.budget_seconds
    now = datetime.now(timezone.utc)
    t0 = now.replace(minute=0, second=0, microsecond=0)
    t_end = t0 + timedelta(days=FORECAST_DAYS)
    log(f"ingest start · window {iso_z(t0)} → {iso_z(t_end)} · budget {args.budget_seconds}s"
        + (" · --force" if args.force else "")
        + (" · --from-cache (offline)" if args.from_cache else ""))

    # Tides are fetched once per unique station (Klitmøller spots share 21009)
    stations = []
    for s in SPOTS:
        if s["tide_station"] not in stations:
            stations.append(s["tide_station"])
    tide_by_station = {}
    for st in stations:
        def _fetch_tides(deadline, st=st):
            tides, high_low, meta = fetch_tides(st, t0, t_end, deadline)
            if not tides:
                raise SourceUnavailable(f"tides/{st}: empty series")
            return {"tides": tides, "high_low": high_low}, meta
        payload, status, meta = get_source(
            f"tides_{st}", _fetch_tides, CACHE_FRESH_TIDES_S,
            t_end - timedelta(days=1), deadline, args.force, f"tides station {st}",
            offline=args.from_cache,
        )
        tide_by_station[st] = (payload, status, meta)

    spots_out, notes = [], []

    # Process spots whose HARMONIE wind cache is missing or stale FIRST.
    # The EDR endpoint is often only responsive in short windows; with a fixed
    # order the limited time budget would always be spent on the first-listed
    # spots and the tail would never get hi-res wind. Output order below
    # stays exactly as configured in SPOTS.
    def _harm_cache_priority(spot):
        c = read_cache(CACHE_DIR / f"harmonie_{spot['id']}.json")
        if not c:
            return (0, float("inf"))          # no cache at all → first
        age = cache_age_s(c)
        return (2, age) if age < CACHE_FRESH_HARMONIE_S else (1, age)

    spot_pos = {s["id"]: i for i, s in enumerate(SPOTS)}
    for spot in sorted(SPOTS, key=_harm_cache_priority):
        sid = spot["id"]
        log(f"spot {sid} ({spot['name']})")

        # --- waves ---
        def _fetch_wam(deadline, spot=spot):
            last_err = "no attempt"
            colls = WAM_COLLECTIONS_BY_SPOT.get(sid, WAM_COLLECTIONS)
            for coll in colls:
                try:
                    rows, meta = fetch_edr_series(
                        coll, spot, WAM_PARAMS, t0, t_end, deadline,
                        required_param="significant-wave-height")
                    if not rows:
                        raise SourceUnavailable("empty feature set")
                    if coll != colls[0]:
                        log(f"  {sid}: used fallback collection {coll}")
                    return rows, meta
                except SourceUnavailable as e:
                    last_err = str(e)
                    log(f"  {sid}: {coll} failed — {last_err}")
            raise SourceUnavailable(last_err)

        wam_rows, wam_status, wam_meta = get_source(
            f"wam2_{sid}", _fetch_wam, CACHE_FRESH_WAM_S,
            t0 + timedelta(hours=24), deadline, args.force, f"{sid} WAM",
            offline=args.from_cache,
        )

        prev_spot = load_previous_spot(sid)
        prev_status = (prev_spot or {}).get("status", {})
        prev_forecast = future_entries((prev_spot or {}).get("forecast"), t0) \
            if prev_status.get("wam") in ("ok", "stale") else []
        # never reuse a previous run whose wave fields are entirely null
        # (broken model run / out-of-domain point looked "ok" at the time)
        if prev_forecast and not any(
            isinstance(e.get("hs"), (int, float)) or isinstance(e.get("swell_h"), (int, float))
            for e in prev_forecast
        ):
            prev_forecast = []
        prev_wind_by_time = {step_key(e["time"]): e for e in prev_forecast} \
            if prev_status.get("harmonie") in ("ok", "stale") else {}

        # --- weather ---
        harm_rows = harm_meta = None
        harm_status = "missing"
        if wam_rows is not None:
            def _fetch_harm(deadline, spot=spot):
                rows, meta = fetch_edr_series(
                    HARMONIE_COLLECTION, spot, HARMONIE_PARAMS,
                    t0, t0 + timedelta(hours=HARMONIE_HOURS), deadline,
                    required_param="wind-speed-10m")
                if not rows:
                    raise SourceUnavailable("empty feature set")
                return rows, meta
            harm_rows, harm_status, harm_meta = get_source(
                f"harmonie_{sid}", _fetch_harm, CACHE_FRESH_HARMONIE_S,
                t0 + timedelta(hours=24), deadline, args.force, f"{sid} HARMONIE",
                offline=args.from_cache,
            )
            if harm_rows is None and prev_wind_by_time:
                harm_status = "stale"  # wind reused from previous forecast.json
                log(f"  {sid}: reusing previous-run wind values (stale)")
            elif harm_rows is None:
                harm_status = "none"
                log(f"  {sid}: no wind data available — rating without wind term")

        # --- compose hourly forecast ---
        if wam_rows is not None:
            hours = compose_hours(spot, wam_rows, harm_rows, prev_wind_by_time)
        elif prev_forecast:
            hours = prev_forecast
            wam_status = "stale"
            if harm_status == "missing":
                harm_status = "stale"
            log(f"  {sid}: reusing previous forecast.json hours (stale)")
            notes.append(f"{spot['name']}: WAM unavailable — reused previous forecast (stale).")
        else:
            hours = sample_hours(spot, t0)
            wam_status = "sample"
            harm_status = "sample" if harm_status == "missing" else harm_status
            log(f"  {sid}: emitting SAMPLE placeholder hours (no live/cached data)")
            notes.append(f"{spot['name']}: no live or cached WAM data — SAMPLE placeholder shown.")

        if wam_status == "stale" and "stale" not in " ".join(notes):
            notes.append(f"{spot['name']}: WAM data is stale (older cached run).")
        if wam_status == "sample":
            pass  # already noted above

        # --- tides ---
        tide_payload, tide_status, tide_meta = tide_by_station[spot["tide_station"]]
        if tide_payload is not None:
            tides, high_low = tide_payload["tides"], tide_payload["high_low"]
        else:
            prev_tides = future_entries((prev_spot or {}).get("tides"), t0)
            prev_hl = future_entries((prev_spot or {}).get("high_low"), t0)
            if prev_tides:
                tides, high_low, tide_status = prev_tides, prev_hl, "stale"
                log(f"  {sid}: reusing previous tide series (stale)")
            else:
                tides, high_low = sample_tides(t0)
                tide_status = "sample"
                log(f"  {sid}: emitting SAMPLE placeholder tides")
                notes.append(f"{spot['name']}: tide predictions unavailable — SAMPLE placeholder shown.")

        spots_out.append({
            "id": sid,
            "name": spot["name"],
            "lat": spot["lat"],
            "lon": spot["lon"],
            "facing_deg": spot["facing_deg"],
            "region": spot["region"],
            "swell_window": list(spot["swell_window"]),
            "offshore_sector": list(spot["offshore_sector"]),
            "tide_station": spot["tide_station"],
            "tide_lat": spot["tide_lat"],
            "tide_lon": spot["tide_lon"],
            "status": {"wam": wam_status, "harmonie": harm_status, "tides": tide_status},
            "meta": {"wam": wam_meta, "harmonie": harm_meta, "tides": tide_meta},
            "forecast": hours,
            "tides": tides,
            "high_low": high_low,
        })

    # restore configured spot order for the output (processing was
    # cache-priority ordered, see above)
    spots_out.sort(key=lambda s: spot_pos[s["id"]])

    # --- roll up per-source status (worst of: sample > stale > none > ok) ---
    severity = {"ok": 0, "none": 1, "stale": 2, "sample": 3, "missing": 3}
    rollup = {}
    for key in ("wam", "harmonie", "tides"):
        worst = "ok"
        for s in spots_out:
            st = s["status"][key]
            if severity.get(st, 3) > severity.get(worst, 0):
                worst = st
        rollup[key] = worst

    output = {
        "generated_at": iso_z(now),
        "sources": rollup,
        "notes": notes,
        "spots": spots_out,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(output, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT_PATH)

    elapsed = int(time.time() - started)
    log(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes) in {elapsed}s")
    log("sources: " + json.dumps(rollup))
    for s in spots_out:
        log(f"  {s['id']}: {s['status']} · {len(s['forecast'])} h forecast · "
            f"{len(s['tides'])} tide pts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
