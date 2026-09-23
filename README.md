# Denmark Surf Forecast

Dedicated surf forecast for 14 Danish spots — DMI WAM waves & wind, DMI tide
predictions, per-spot ratings tuned to local conditions, a regional model-field
map, and a 16-day ECMWF/GFS long-range outlook.

Data sources: [DMI](https://www.dmi.dk/fri-data) Forecast Data (WAM ·
HARMONIE-DINI) & OceanObs · long range: ECMWF IFS/WAM & NOAA GFS/GFS-Wave via
[Open-Meteo](https://open-meteo.com/). Ratings are a local heuristic, not an
official DMI product.

## Publishing — Vercel (same host as StretchFlow)

The site is fully static; `frontend/vercel.json` already pins the Vite build
settings. One-time login, then deploys are one command:

```bash
cd frontend
npx vercel login          # one-time, opens browser / email verification
npx vercel --prod --yes   # first run creates the project; later runs redeploy
```

The local 4×/day data-refresh task (Kimi cron) can redeploy automatically
after each successful refresh, so the public site stays current while the
laptop is on.

### Alternative: GitHub Pages (laptop-independent)

`.github/workflows/refresh.yml` runs the full data pipeline in the cloud
4×/day and deploys to GitHub Pages — the site keeps updating even when this
machine is off. Public repo recommended (free Actions minutes). Steps:
create repo → push → Settings → Pages → Source "GitHub Actions" → run the
workflow once. Site: `https://<your-user>.github.io/<repo>/`.

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
