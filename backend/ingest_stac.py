#!/usr/bin/env python3
"""
ingest_stac.py — WAM wave + marine-wind ingestion via DMI's STAC bulk API
==========================================================================

Why this exists
---------------
The forecast EDR ``/position`` endpoint (used by ``ingest.py``) is heavily
congested server-side (HTTP 429 "Server is busy") for hours at a time. DMI
publishes the very same model output as GRIB files through the STAC API —
one small file per forecast timestep, hosted on S3 — which is the channel
DMI itself recommends for larger data volumes and which does not share the
EDR congestion.

What it does
------------
1. Discovers the latest WAM ``wam_dw`` model run (Danish Waters domain,
   bbox [7E..16E, 53N..60N] at ~1.1 x 1.9 km — covers ALL configured spots).
2. Downloads one ~3.6 MB GRIB per hourly timestep (politely: <= 1 file/s,
   DMI fair-use for bulk downloads) into ``backend/cache/grib/wam_dw/<run>/``.
3. Parses each GRIB once with cfgrib/eccodes and extracts the 8 parameters
   used downstream at every spot's extraction point (nearest WET grid cell —
   WAM is land-masked, so coastal spots snap to the closest sea cell).
4. Writes per-spot caches ``backend/cache/wam2_<spot>.json`` in exactly the
   shape ``ingest.py`` produces from EDR, so the whole downstream pipeline
   (HARMONIE wind enhancement, rating, composition) works unchanged — run
   ``ingest.py`` afterwards and it will see fresh caches and skip EDR waves.

HARMONIE wind is NOT fetched here: its GRIBs are ~620 MB per timestep, so
hi-res wind/gusts/temperature remain an EDR-side enhancement handled by
``ingest.py`` whenever that endpoint is responsive. Without it, the WAM
marine 10 m wind extracted here still covers the full 5-day horizon.

Resumability
------------
Both downloads and per-step extraction results are cached on disk
(``extract/<step>.json``), so an interrupted run loses nothing: re-running
skips straight past completed steps. Old model runs are pruned.

Usage
-----
    python backend/ingest_stac.py                    # full horizon (~4-6 min)
    python backend/ingest_stac.py --max-steps 36     # quick partial run
    python backend/ingest_stac.py --force            # redownload everything
    python backend/ingest.py --budget-seconds 240    # then: wind/tides/compose
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest  # shared SPOTS config, cache helpers, iso helpers

STAC_BASE = "https://opendataapi.dmi.dk/v1/forecastdata"
# wam_dw "Danish Waters": 0.01° x 0.0167° (~1.1 x 1.9 km), bbox 7-16E / 53-60N.
# Verified from the raw GRIBs to cover ALL spots incl. the open Skagerrak
# coast (Klitmoller, Hvide Sande wet) — the EDR /position endpoint falsely
# returned all-null there. ~5x finer than wam_nsb, which we used before.
WAM_COLLECTION = "wam_dw"
GRIB_ROOT = ingest.CACHE_DIR / "grib" / WAM_COLLECTION

# GRIB shortName -> EDR-style parameter name used by the whole pipeline
PARAM_MAP = {
    "swh": "significant-wave-height",
    "shts": "significant-totalswell-height",
    "mpts": "mean-totalswell-period",
    "pp1d": "peak-wave-period",      # Tp — used by the rating (surf quality)
    "mdts": "mean-totalswell-dir",
    "shww": "significant-windwave-height",
    "mwd": "mean-wave-dir",
    "wind": "wind-speed",
    "dwi": "wind-dir",
}

DOWNLOAD_PACE_S = 1.05      # DMI fair use: max ~1 file/s for bulk downloads
HTTP_TRIES = 4
FORECAST_HOURS = 121
LOOKBACK_HOURS = 3          # include the current, partially-elapsed hours


def log(msg: str) -> None:
    ingest.log(msg)


def iso_z(dt: datetime) -> str:
    return ingest.iso_z(dt)


def http_json(url: str, what: str, timeout: int = 30):
    """Small retrying JSON GET for the STAC API (not the congested one)."""
    last = "no attempt"
    for attempt in range(1, HTTP_TRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": ingest.USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - log and retry
            last = f"{type(e).__name__}: {e}"
            log(f"  {what}: {last} — attempt {attempt}/{HTTP_TRIES}")
            time.sleep(2.5 * attempt)
    raise SystemExit(f"FATAL: {what} failed after {HTTP_TRIES} tries ({last})")


def discover_latest_run() -> str:
    """The farthest-future step always belongs to the latest complete run."""
    data = http_json(
        f"{STAC_BASE}/collections/{WAM_COLLECTION}/items"
        f"?limit=1&sortorder=datetime,DESC", "stac latest item")
    run = data["features"][0]["properties"]["modelRun"]
    log(f"latest {WAM_COLLECTION} model run: {run}")
    return run


def list_run_items(run_iso: str) -> dict:
    """{valid_time_iso: {"href": s3_url, "id": filename}} for one model run."""
    data = http_json(
        f"{STAC_BASE}/collections/{WAM_COLLECTION}/items"
        f"?modelRun={run_iso}&limit=400",
        f"stac items {run_iso}")
    items = {}
    for f in data.get("features", []):
        items[f["properties"]["datetime"]] = {
            "href": f["asset"]["data"]["href"], "id": f["id"]}
    log(f"run {run_iso}: {len(items)} timestep files listed")
    return items


def needed_steps(t0: datetime) -> list:
    start = t0 - timedelta(hours=LOOKBACK_HOURS)
    end = t0 + timedelta(hours=FORECAST_HOURS)
    out = []
    t = start
    while t <= end:
        out.append(t.strftime("%Y-%m-%dT%H:%M:%SZ"))
        t += timedelta(hours=1)
    return out


def download(url: str, dest: Path, timeout: int = 90) -> bool:
    tmp = dest.with_suffix(".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ingest.USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            expected = int(resp.headers.get("Content-Length") or 0)
            data = resp.read()
        if expected and len(data) != expected:
            log(f"  download truncated ({len(data)}/{expected}) — {dest.name}")
            return False
        tmp.write_bytes(data)
        tmp.replace(dest)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"  download failed: {type(e).__name__}: {e} — {dest.name}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# GRIB extraction
# ---------------------------------------------------------------------------

class GribExtractor:
    """Opens WAM GRIB step files; extracts all spots from each field."""

    def __init__(self, first_file: Path):
        import numpy as np
        import xarray as xr

        self.np = np
        self.xr = xr
        ds = xr.open_dataset(str(first_file), engine="cfgrib",
                             backend_kwargs={"indexpath": ""})
        self.lats = ds.latitude.values
        self.lons = ds.longitude.values
        swh = ds["swh"].values
        ds.close()
        self.idx = {}
        for spot in ingest.SPOTS:
            j, i, d = self._nearest_wet(swh, spot["lat"], spot["lon"])
            self.idx[spot["id"]] = (j, i)
            note = "" if d < 0.05 else f"  (note: {d * 111:.1f} km from requested point)"
            log(f"  grid {spot['id']}: cell ({j},{i}) "
                f"lat {self.lats[j]:.3f} lon {self.lons[i]:.3f}{note}")

    def _nearest_wet(self, field, lat: float, lon: float):
        """Nearest non-NaN (sea) cell to (lat, lon), expanding-ring search."""
        np = self.np
        j0 = int(np.argmin(np.abs(self.lats - lat)))
        i0 = int(np.argmin(np.abs(self.lons - lon)))
        coslat = math.cos(math.radians(lat))
        best = None  # (j, i, dist_deg)
        for r in range(0, 12):
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    if max(abs(dj), abs(di)) != r:
                        continue
                    j, i = j0 + dj, i0 + di
                    if not (0 <= j < len(self.lats) and 0 <= i < len(self.lons)):
                        continue
                    if np.isfinite(field[j, i]):
                        d = math.hypot(self.lats[j] - lat, (self.lons[i] - lon) * coslat)
                        if best is None or d < best[2]:
                            best = (j, i, d)
            if best is not None and r >= 1:
                break  # one extra ring beyond the first hit is enough
        if best is None:
            raise SystemExit(f"FATAL: no wet WAM cell near {lat},{lon}")
        return best

    def extract(self, path: Path):
        """-> (step_key, {spot_id: {param: value}}) for one GRIB file."""
        np = self.np
        ds = self.xr.open_dataset(str(path), engine="cfgrib",
                                  backend_kwargs={"indexpath": ""})
        vt = ds.valid_time.values  # numpy datetime64
        epoch_s = float((vt - np.datetime64("1970-01-01T00:00:00"))
                        / np.timedelta64(1, "s"))
        dt = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
        key = dt.strftime("%Y-%m-%dT%H:%MZ")
        arrs = {}
        for v in PARAM_MAP:
            if v in ds:
                arrs[v] = ds[v].values
        rows = {}
        for sid, (j, i) in self.idx.items():
            row = {"step": key}
            for v, param in PARAM_MAP.items():
                a = arrs.get(v)
                if a is None:
                    row[param] = None
                    continue
                val = a[j, i]
                row[param] = round(float(val), 4) if np.isfinite(val) else None
            rows[sid] = row
        ds.close()
        return key, rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="DK Surf — WAM ingestion via DMI STAC/GRIB")
    ap.add_argument("--budget-seconds", type=int, default=560,
                    help="wall-clock budget for downloads+parsing (default 560)")
    ap.add_argument("--max-steps", type=int, default=0,
                    help="process at most N timesteps (testing / chunked runs)")
    ap.add_argument("--force", action="store_true",
                    help="redownload + reparse even when cached")
    ap.add_argument("--run", default=None, help="override model run ISO, e.g. 2026-07-19T18:00:00Z")
    args = ap.parse_args()

    started = time.time()
    deadline = started + args.budget_seconds
    now = datetime.now(timezone.utc)
    t0 = now.replace(minute=0, second=0, microsecond=0)
    log(f"stac-ingest start · budget {args.budget_seconds}s"
        + (" · --force" if args.force else ""))

    run_iso = args.run or discover_latest_run()
    items = list_run_items(run_iso)
    run_tag = run_iso.replace(":", "").replace("-", "")
    run_dir = GRIB_ROOT / run_tag
    extract_dir = run_dir / "extract"
    run_dir.mkdir(parents=True, exist_ok=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    steps = [s for s in needed_steps(t0) if s in items]
    missing = [s for s in needed_steps(t0) if s not in items]
    if missing:
        log(f"note: {len(missing)} needed steps not in run (edge of horizon) — skipped")
    if args.max_steps:
        steps = steps[: args.max_steps]
    log(f"processing {len(steps)} timesteps from run {run_iso}")

    # per-spot assembled rows: {sid: {step_key: row}}
    assembled = {s["id"]: {} for s in ingest.SPOTS}
    extractor = None
    n_dl = n_parse = n_cached = 0
    last_dl = 0.0

    for n, step_iso in enumerate(steps, 1):
        if deadline - time.time() < 12:
            log(f"budget nearly exhausted after {n - 1} steps — stopping early")
            break
        info = items[step_iso]
        grib_path = run_dir / info["id"]
        extract_path = extract_dir / (step_iso.replace(":", "") + ".json")

        # 1) extraction cache hit? (cheap path for resumed runs)
        if not args.force and extract_path.exists():
            try:
                payload = json.loads(extract_path.read_text(encoding="utf-8"))
                key = payload["step"]
                for sid, row in payload["spots"].items():
                    assembled[sid][key] = row
                n_cached += 1
                continue
            except Exception:
                pass  # corrupt cache -> reparse below

        # 2) ensure GRIB downloaded
        if args.force or not grib_path.exists() or grib_path.stat().st_size == 0:
            pace = DOWNLOAD_PACE_S - (time.time() - last_dl)
            if pace > 0:
                time.sleep(pace)
            last_dl = time.time()
            if not download(info["href"], grib_path):
                continue  # skip this step; keep going
            n_dl += 1

        # 3) parse + extract all spots
        try:
            if extractor is None:
                log("building spot grid index (nearest wet WAM cell per spot):")
                extractor = GribExtractor(grib_path)
            key, rows = extractor.extract(grib_path)
        except Exception as e:  # noqa: BLE001
            log(f"  parse failed {grib_path.name}: {type(e).__name__}: {e}")
            continue
        n_parse += 1
        extract_path.write_text(json.dumps({"step": key, "spots": rows}),
                                encoding="utf-8")
        for sid, row in rows.items():
            assembled[sid][key] = row
        if n % 20 == 0 or n == len(steps):
            log(f"  progress {n}/{len(steps)} steps "
                f"(dl {n_dl}, parsed {n_parse}, cache-hits {n_cached})")

    # 4) write per-spot caches in ingest.py's exact shape
    wrote = 0
    for spot in ingest.SPOTS:
        sid = spot["id"]
        rows = [assembled[sid][k] for k in sorted(assembled[sid])]
        if not rows:
            log(f"  {sid}: no data extracted — cache not written")
            continue
        meta = {
            "collection": f"{WAM_COLLECTION}·stac-grib",
            "modelRun": run_iso,
            "fetched_at": iso_z(now),
            "steps": len(rows),
            "first_step": ingest.key_to_iso(rows[0]["step"]),
            "last_step": ingest.key_to_iso(rows[-1]["step"]),
        }
        ingest.write_cache(ingest.CACHE_DIR / f"wam2_{sid}.json",
                           {"meta": meta, "payload": rows})
        wrote += 1

    # 5) prune old model runs (keep the 2 newest)
    runs = sorted([p for p in GRIB_ROOT.iterdir() if p.is_dir()], reverse=True)
    for old in runs[2:]:
        if old.name == run_tag:
            continue
        for f in sorted(old.rglob("*"), reverse=True):
            if f.is_file():
                f.unlink(missing_ok=True)
            elif f.is_dir():
                try:
                    f.rmdir()
                except OSError:
                    pass
        try:
            old.rmdir()
        except OSError:
            pass
        log(f"pruned old run {old.name}")

    elapsed = int(time.time() - started)
    log(f"done in {elapsed}s · downloaded {n_dl}, parsed {n_parse}, "
        f"cache-hits {n_cached} · caches written for {wrote}/{len(ingest.SPOTS)} spots")
    if wrote:
        log("next: python backend/ingest.py --budget-seconds 240  "
            "(wind/tides/composition — WAM EDR will be skipped via fresh cache)")
    return 0 if wrote else 1


if __name__ == "__main__":
    sys.exit(main())
