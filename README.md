# Denmark Surf Forecast

Dedicated surf forecast for 14 Danish spots — DMI WAM waves & wind, DMI tide
predictions, per-spot ratings tuned to local conditions, a regional model-field
map, and a 16-day ECMWF/GFS long-range outlook.

Data sources: [DMI](https://www.dmi.dk/fri-data) Forecast Data (WAM ·
HARMONIE-DINI) & OceanObs · long range: ECMWF IFS/WAM & NOAA GFS/GFS-Wave via
[Open-Meteo](https://open-meteo.com/). Ratings are a local heuristic, not an
official DMI product.

## Publishing (one-time setup, ~10 min)

The site is fully static; the data pipeline runs in GitHub Actions 4×/day and
deploys to GitHub Pages automatically (workflow already included at
`.github/workflows/refresh.yml`).

1. **Create the repo** on [github.com/new](https://github.com/new)
   - Name: e.g. `dk-surf-forecast`
   - **Public** recommended: GitHub Actions is free for public repos
     (the 4×/day pipeline uses ~2–3 000 min/month, above the free
     private-repo allowance).
2. **Push this project** (from the project root):
   ```bash
   git remote add origin https://github.com/<your-user>/dk-surf-forecast.git
   git push -u origin main
   ```
3. **Enable Pages**: repo → **Settings → Pages → Source: "GitHub Actions"**.
4. **First deploy**: repo → **Actions → "Refresh forecast data & deploy" →
   Run workflow**. The first run downloads the full GRIB set (~460 MB) and
   takes ~15–25 min; later runs are faster.
5. Site goes live at `https://<your-user>.github.io/dk-surf-forecast/`
   (custom domain: Settings → Pages → Custom domain — the build is
   sub-path safe, no config change needed).

After that the site updates itself 4×/day at 03:14 / 09:14 / 15:14 / 21:14 UTC
(≈3 h after each DMI model cycle). Every run is visible in the Actions tab,
including which data sources succeeded.

## Local development

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

Refresh data locally (same commands the cloud workflow runs):

```bash
python backend/ingest_stac.py --budget-seconds 1500   # DMI WAM GRIB → spot caches
python backend/export_fields.py                       # regional map fields
python backend/ingest_longrange.py                    # 16-day ECMWF/GFS
python backend/ingest.py --budget-seconds 600         # tides + compose forecast.json
```

`DEVELOPMENT_PLAN.md` has the full architecture roadmap (backend API, mobile
apps).
