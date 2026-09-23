#!/usr/bin/env python3
"""
export_fields.py — regional gridded forecast fields for the map view
====================================================================

Reads the LOCAL GRIB cache written by ``ingest_stac.py``
(``backend/cache/grib/wam_dw/<modelRun>/*.grib``) — no network access
needed — and exports a compact regional subset around the configured surf
spots to ``frontend/public/data/fields.json``. The ~1 km source grid is
block-downsampled (``--downsample``, default ×3) to keep the web payload
small; direction fields use circular means.

Per 3-hourly frame, for every grid cell in the region:
  swh   significant wave height (m)          — wave-height contours
  mpts  mean total-swell period (s)          — wave-period contours
  mdir  wave direction (°, swell dir, falling back to mean wave dir)
  wind  10 m wind speed (m/s)                — wind-speed contours
  wdir  10 m wind direction (°, FROM)

Grid meta (lat0/lon0/dlat/dlon, row-major, row 0 = southernmost) lets the
frontend project cells to screen. Land cells are null (WAM land mask).

Usage
-----
    python backend/export_fields.py                 # 3-hourly to +120 h
    python backend/export_fields.py --step-hours 1  # hourly (bigger file)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest  # SPOTS config, paths, iso helpers

GRIB_ROOT = ingest.CACHE_DIR / "grib" / "wam_dw"
OUT_PATH = ingest.ROOT / "frontend" / "public" / "data" / "fields.json"

MARGIN_DEG = 0.45          # region padding around the spots bounding box
MAX_FRAMES = 200


def log(msg: str) -> None:
    ingest.log(msg)


def latest_run_dir() -> Path:
    runs = sorted([p for p in GRIB_ROOT.iterdir() if p.is_dir()], reverse=True)
    if not runs:
        raise SystemExit("FATAL: no GRIB cache found — run ingest_stac.py first")
    return runs[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Export regional fields for the map view")
    ap.add_argument("--step-hours", type=int, default=3,
                    help="frame spacing in hours (default 3)")
    ap.add_argument("--hours", type=int, default=120, help="horizon (default 120)")
    ap.add_argument("--downsample", type=int, default=3,
                    help="block-averaging factor for the ~1 km source grid "
                         "(default 3 → ~3.3 x 5.6 km web grid)")
    args = ap.parse_args()

    started = time.time()
    run_dir = latest_run_dir()
    log(f"exporting fields from {run_dir.name} (every {args.step_hours} h)")

    import numpy as np
    import xarray as xr

    files = sorted(run_dir.glob("*.grib"))
    if not files:
        raise SystemExit(f"FATAL: no .grib files in {run_dir}")

    # --- region: spots bbox + margin, snapped to the model grid ---
    lats = [s["lat"] for s in ingest.SPOTS]
    lons = [s["lon"] for s in ingest.SPOTS]
    south, north = min(lats) - MARGIN_DEG, max(lats) + MARGIN_DEG
    west, east = min(lons) - MARGIN_DEG, max(lons) + MARGIN_DEG

    first = xr.open_dataset(str(files[0]), engine="cfgrib",
                            backend_kwargs={"indexpath": ""})
    glat, glon = first.latitude.values, first.longitude.values
    lat_desc = bool(glat[0] > glat[-1])  # WAM GRIBs scan north→south
    def _lo_hi(arr, lo, hi):
        a = int(np.argmin(np.abs(arr - lo)))
        b = int(np.argmin(np.abs(arr - hi)))
        return min(a, b), max(a, b) + 1
    j0, j1 = _lo_hi(glat, south, north)
    i0, i1 = _lo_hi(glon, west, east)
    j0, j1 = max(0, j0), min(len(glat), j1)
    i0, i1 = max(0, i0), min(len(glon), i1)
    # Downsampled web grid: block-mean coordinates from a coarsened probe
    F = args.downsample
    probe = first[["swh"]].isel(latitude=slice(j0, j1), longitude=slice(i0, i1))
    probe = probe.coarsen(latitude=F, longitude=F, boundary="trim").mean(skipna=True)
    reg_lat, reg_lon = probe.latitude.values, probe.longitude.values
    if lat_desc:
        reg_lat = reg_lat[::-1]  # output row 0 = southernmost
    ny, nx = len(reg_lat), len(reg_lon)
    log(f"region: lat {reg_lat[0]:.3f}..{reg_lat[-1]:.3f} "
        f"lon {reg_lon[0]:.3f}..{reg_lon[-1]:.3f} · grid {ny}×{nx} "
        f"(downsampled ×{F} from ~1 km source)")
    first.close()

    import warnings
    warnings.filterwarnings("ignore", message=".*All-NaN.*")

    now = datetime.now(timezone.utc)
    t0 = now.replace(minute=0, second=0, microsecond=0)
    t0_epoch = t0.timestamp()

    frames = []
    for f in files:
        if len(frames) >= MAX_FRAMES:
            break
        try:
            ds = xr.open_dataset(str(f), engine="cfgrib",
                                 backend_kwargs={"indexpath": ""})
        except Exception as e:  # noqa: BLE001
            log(f"  skip unreadable {f.name}: {e}")
            continue
        vt = ds.valid_time.values
        epoch = float((vt - np.datetime64("1970-01-01T00:00:00"))
                      / np.timedelta64(1, "s"))
        hrs = (epoch - t0_epoch) / 3600.0
        if hrs < -3.5 or hrs > args.hours + 0.5 or hrs % args.step_hours != 0:
            ds.close()
            continue

        DIR_VARS = {"mdts", "mwd", "dwi"}

        def field(var):
            """Region slice → block-downsampled array (row 0 = southernmost).
            Scalars use a plain nan-mean; directions use a circular (vector)
            mean so 350° + 10° averages to 0°, not 180°."""
            if var not in ds:
                return None
            da = ds[var].isel(latitude=slice(j0, j1), longitude=slice(i0, i1))
            if var in DIR_VARS:
                r = np.deg2rad(da.astype("float64"))
                s = np.sin(r).coarsen(latitude=F, longitude=F, boundary="trim").mean(skipna=True)
                c = np.cos(r).coarsen(latitude=F, longitude=F, boundary="trim").mean(skipna=True)
                a = (np.rad2deg(np.arctan2(s.values, c.values)) + 360.0) % 360.0
            else:
                a = da.coarsen(latitude=F, longitude=F, boundary="trim").mean(skipna=True).values
            if lat_desc:
                a = a[::-1, :]  # row 0 = southernmost
            return a

        swh = field("swh")
        mpts = field("mpts")
        mdts = field("mdts")
        mwd = field("mwd")
        wind = field("wind")
        dwi = field("dwi")
        ds.close()

        def pack(a, nd):
            if a is None:
                return [None] * (ny * nx)
            out = []
            for v in a.reshape(-1):
                out.append(round(float(v), nd) if np.isfinite(v) else None)
            return out

        # wave direction: total-swell direction, fall back to mean wave dir
        mdir = None
        if mdts is not None or mwd is not None:
            mdir = np.where(
                (mdts is not None) & np.isfinite(mdts),
                mdts if mdts is not None else np.nan,
                mwd if mwd is not None else np.nan)

        frames.append({
            "time": datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "swh": pack(swh, 2),
            "mpts": pack(mpts, 1),
            "mdir": pack(mdir, 1),
            "wind": pack(wind, 1),
            "wdir": pack(dwi, 1),
        })

    frames.sort(key=lambda fr: fr["time"])
    if not frames:
        raise SystemExit("FATAL: no frames in the requested window")

    out = {
        "generated_at": ingest.iso_z(now),
        "model_run": run_dir.name,
        "grid": {
            "lat0": float(reg_lat[0]), "dlat": float(reg_lat[1] - reg_lat[0]),
            "lon0": float(reg_lon[0]), "dlon": float(reg_lon[1] - reg_lon[0]),
            "ny": ny, "nx": nx,
            "layout": "row-major, row 0 = southernmost, col 0 = westernmost",
        },
        "units": {"swh": "m", "mpts": "s", "mdir": "deg_from",
                  "wind": "m/s", "wdir": "deg_from"},
        "frames": frames,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT_PATH)
    log(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes) · "
        f"{len(frames)} frames · {int(time.time() - started)}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
