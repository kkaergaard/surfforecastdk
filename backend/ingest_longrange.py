#!/usr/bin/env python3
"""Long-range (day 5-16) surf guidance for all DK spots.

Sources (via Open-Meteo, no API key required):
  - ECMWF IFS 0.25 deg wind  (10 days)  + ECMWF WAM 0.25 deg waves (10 days)
  - NOAA  GFS seamless wind  (16 days)  + GFS-Wave 0.25 deg waves  (16 days)

The 5-day high-res DMI WAM forecast remains the primary product; this file
(longrange.json) extends the outlook so trips 1-2 weeks out can be planned.
Both model families are kept side by side: where they agree, confidence is
higher; where they diverge, treat the outlook as speculative.

Period caveat: the long-range APIs expose *mean* wave period, while the
spot rating was calibrated on *peak* period (pp1d). We approximate
Tp ~= 1.2 x Tm for the rating only; raw mean periods are stored untouched.
"""

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest import SPOTS, surf_rating  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "frontend" / "public" / "data" / "longrange.json"

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
FC_URL = "https://api.open-meteo.com/v1/forecast"

WAVE_VARS = [
    "wave_height", "wave_period", "wave_direction",
    "swell_wave_height", "swell_wave_period", "swell_wave_direction",
    "wind_wave_height", "wind_wave_period", "wind_wave_direction",
]
WIND_VARS = ["wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"]

# (key used in output, open-meteo model name)
WAVE_MODELS = [("ecmwf", "ecmwf_wam025"), ("gfs", "ncep_gfswave025")]
WIND_MODELS = [("ecmwf", "ecmwf_ifs025"), ("gfs", "gfs_seamless")]

TP_FROM_TM = 1.2          # mean -> peak period approximation (rating only)
REQUEST_TIMEOUT_S = 30
POLITE_PAUSE_S = 1.1
KEEP_EVERY_H = 3          # store every 3rd hour


def log(msg):
    print(f"[longrange {datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)


def fetch_json(url, params):
    qs = urllib.parse.urlencode(params)
    last = None
    for attempt in (1, 2, 3):
        try:
            req = urllib.request.Request(f"{url}?{qs}", headers={"User-Agent": "dk-surf-forecast/1.0"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # transient SSL/timeout blips happen (GitHub runners 2026-09-23)
            last = e
            time.sleep(5 * attempt)
    raise last


def model_series(hourly, var, model_name):
    """Open-Meteo suffixes variables with the model name when several models
    are requested; fall back to the plain name for single-model replies."""
    return hourly.get(f"{var}_{model_name}") or hourly.get(var)


def deg_ok(x):
    return x if (x is not None and 0 <= x <= 360) else None


def rate(hs, tm, swell_dir, wind_ms, wind_dir, spot):
    if hs is None or wind_ms is None:
        return None, None
    tp = tm * TP_FROM_TM if tm is not None else None
    try:
        score, label, _capped, _note, _wc = surf_rating(
            hs=hs, tp=tp, swell_dir_deg=deg_ok(swell_dir),
            wind_ms=wind_ms, wind_dir_deg=deg_ok(wind_dir), spot=spot)
        return round(score, 1), label
    except Exception:
        return None, None


def build_spot(spot):
    lat, lon = spot["lat"], spot["lon"]
    marine = fetch_json(MARINE_URL, {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(WAVE_VARS),
        "models": ",".join(m for _, m in WAVE_MODELS),
        "forecast_days": 16, "timezone": "UTC",
    })
    time.sleep(POLITE_PAUSE_S)
    wind = fetch_json(FC_URL, {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(WIND_VARS),
        "models": ",".join(m for _, m in WIND_MODELS),
        "forecast_days": 16, "timezone": "UTC", "wind_speed_unit": "ms",
    })

    mh, wh = marine.get("hourly", {}), wind.get("hourly", {})
    times = mh.get("time") or wh.get("time") or []
    if not times:
        raise RuntimeError("no hourly time axis returned")

    # wind lookup by timestamp (wind API axis can differ from marine axis)
    wind_by_t = {}
    for i, t in enumerate(wh.get("time", [])):
        wind_by_t[t] = i

    out = []
    for i, t in enumerate(times):
        hour = datetime.fromisoformat(t).hour
        if hour % KEEP_EVERY_H != 0:
            continue
        row = {"time": t + "Z"}
        wi = wind_by_t.get(t)
        for key, wave_model in WAVE_MODELS:
            wind_model = dict(WIND_MODELS)[key]
            hs = (model_series(mh, "wave_height", wave_model) or [None] * len(times))[i]
            tm = (model_series(mh, "wave_period", wave_model) or [None] * len(times))[i]
            wdir = (model_series(mh, "wave_direction", wave_model) or [None] * len(times))[i]
            s_h = (model_series(mh, "swell_wave_height", wave_model) or [None] * len(times))[i]
            s_p = (model_series(mh, "swell_wave_period", wave_model) or [None] * len(times))[i]
            s_d = (model_series(mh, "swell_wave_direction", wave_model) or [None] * len(times))[i]
            ww_h = (model_series(mh, "wind_wave_height", wave_model) or [None] * len(times))[i]
            w_ms = w_dir = gust = None
            if wi is not None:
                w_ms = (model_series(wh, "wind_speed_10m", wind_model) or [None] * (wi + 1))[wi]
                w_dir = (model_series(wh, "wind_direction_10m", wind_model) or [None] * (wi + 1))[wi]
                gust = (model_series(wh, "wind_gusts_10m", wind_model) or [None] * (wi + 1))[wi]
            score, label = rate(hs, tm, s_d if s_d is not None else wdir, w_ms, w_dir, spot)
            row[key] = {
                "hs": round(hs, 2) if hs is not None else None,
                "tm_s": round(tm, 1) if tm is not None else None,
                "wave_dir": round(wdir) if wdir is not None else None,
                "swell_h": round(s_h, 2) if s_h is not None else None,
                "swell_p": round(s_p, 1) if s_p is not None else None,
                "swell_dir": round(s_d) if s_d is not None else None,
                "windwave_h": round(ww_h, 2) if ww_h is not None else None,
                "wind_ms": round(w_ms, 1) if w_ms is not None else None,
                "wind_dir": round(w_dir) if w_dir is not None else None,
                "gust_ms": round(gust, 1) if gust is not None else None,
                "rating": score, "rating_label": label,
            }
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-seconds", type=float, default=240)
    args = ap.parse_args()
    t0 = time.time()

    spots_out, errors = [], []
    for n, spot in enumerate(SPOTS, 1):
        if time.time() - t0 > args.budget_seconds:
            errors.append(f"budget exhausted after {n - 1} spots")
            break
        try:
            fc = build_spot(spot)
            spots_out.append({
                "id": spot["id"], "name": spot["name"],
                "lat": spot["lat"], "lon": spot["lon"],
                "region": spot.get("region"), "forecast": fc,
            })
            log(f"{n}/{len(SPOTS)} {spot['name']}: {len(fc)} 3-hourly points")
        except Exception as e:  # keep going: one spot's failure must not kill the file
            errors.append(f"{spot['name']}: {e}")
            log(f"{n}/{len(SPOTS)} {spot['name']}: FAILED {e}")
        time.sleep(POLITE_PAUSE_S)

    if not spots_out:
        log("ERROR: no spots fetched — keeping existing longrange.json")
        return 1

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Open-Meteo: ECMWF IFS/WAM 0.25° (10 d) + NOAA GFS/GFS-Wave 0.25° (16 d)",
        "note": ("Long-range guidance beyond the 5-day DMI WAM forecast. Periods are mean "
                 "wave period; ratings approximate Tp ≈ 1.2 × Tm. Treat days 5-10 as "
                 "indicative, days 10+ as speculative, especially where ECMWF and GFS disagree."),
        "errors": errors,
        "spots": spots_out,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"wrote {OUT.name}: {len(spots_out)}/{len(SPOTS)} spots, "
        f"{OUT.stat().st_size / 1e6:.1f} MB, errors={len(errors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
