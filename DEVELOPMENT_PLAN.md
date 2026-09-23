# Denmark Surf Forecast — Development Plan

**Vision:** A Surfline-style surf forecast platform dedicated to Denmark — web, Android and iOS —
built on free, open data from the Danish Meteorological Institute (DMI).

---

## 1. What we are building

A three-client platform showing dedicated, spot-level surf forecasts for Denmark:

| Client | Purpose |
|---|---|
| **Web app** (responsive, PWA) | Primary surface: map, spot pages, charts, detailed forecasts |
| **Android app** | Favorites, push alerts, on-the-beach use, offline cache |
| **iOS app** | Same feature set as Android |

Core features (benchmarked against surfline.com):

- Spot map + spot list with current conditions and a 0–5 star surf rating
- 5–7 day hourly forecast per spot: wave height, period, direction, wind speed/direction/gusts
- Tide curves and high/low tide times
- Water temperature, air temperature, sunrise/sunset
- Wind and wave "now" observations from nearby DMI stations
- Favorites + push notifications ("Klitmøller goes 4 stars tomorrow morning")
- Later: webcam integration, user surf reports, premium tier

---

## 2. Data foundation — what DMI actually provides

All sources below are free, open (High Value Data, may be used and redistributed with attribution),
REST/JSON, WGS84, with a fair-use limit of 500 requests / 5 seconds.

### 2.1 Forecast Data EDR API — `https://dmigw.govcloud.dk/v1/forecastedr`
Point/area queries returning GeoJSON/CoverageJSON. Latest 24 h of model runs. CORS-enabled.

| DMI model | Collection(s) | What it gives us |
|---|---|---|
| **HARMONIE DINI** (weather, 2 km grid) | `harmonie_dini_sf` | Wind speed/direction/gust at 10 m, air temperature, cloud cover, precipitation |
| **WAM** (wave model) | `wam_dw` (Danish Waters), `wam_nsb` (North Sea–Baltic), `wam_natlant` (North Atlantic) | Significant wave height, wave period, wave direction, surface wind over ocean |
| **DKSS** (storm surge) | `dkss_nsbs`, `dkss_idw`, `dkss_ws`, `dkss_if`, `dkss_lf`, `dkss_lb` | Water level forecast incl. surge (relevant for exposed shallow spots) |

- New runs 4×/day; WAM files complete ~3 h after run start → ingestion schedule: fetch at ~03:15, 09:15, 15:15, 21:15 UTC.
- The EDR API rate-limits large responses (HTTP 413) → query **per spot point**, not full grids, at first. (Bulk GRIB via the STAC API is the upgrade path if we later want full-map rendering.)

### 2.2 Oceanographic Observation + Tidewater API — `https://opendataapi.dmi.dk/v2/oceanObs`
| Collection | What it gives us |
|---|---|
| `station` + `observation` | 10-min sea level (`sealev_dvr`) and water temperature (`tw`) from ~coastal tide gauges |
| `tidewater` + `tidewaterstation` | **Official tide predictions** for 204 Danish stations (10-min / high / low values), covering current + next year |

### 2.3 Meteorological Observations (metObs) — `https://opendataapi.dmi.dk/v2/metObs`
Real-time coastal wind observations (e.g. Hanstholm, Hvide Sande, Thyborøn, Anholt) for the "now" panel and for validating/calibrating spot ratings.

### 2.4 Gaps and complementary sources (optional, non-DMI)
- **Nearshore wave transformation:** WAM is a regional deep/intermediate-water model; it does not model local shoaling/refraction at the beach. Mitigation: per-spot exposure metadata + shadowing coefficients (Phase 0 calibration), optionally a SWAN downscaling layer later.
- **Longer range / ensemble backup:** Open-Meteo Marine API (free) as a cross-check/fallback.
- **Webcams:** no DMI source; integrate public harbor/coastal cams (e.g. Kystdirektoratet cams) in Phase 3.
- **Basemaps:** Danish Dataforsyningen (free national maps/orthophotos) or MapLibre + OSM tiles.

### 2.5 Starter spot catalog (~15 spots)
- **West coast (North Sea):** Klitmøller, Vorupør, Hanstholm, Agger, Krik Vig, Hvide Sande (North & South piers), Bjerregård, Henne Strand, Blåvand
- **Kattegat / East Jutland:** Løkken, Skagen (Grenen), Grenaa, Tisvildeleje (N Zealand)
- **Bornholm:** Dueodde, Balka (windswell spots)

Each spot record: position, facing direction, optimal swell window, offshore-wind sector, best tide (station + window), skill level, break type, access notes. **This spot metadata is the product's core IP** — it turns raw model data into a useful surf forecast.

---

## 3. System architecture

```
                        ┌──────────────────────── DMI APIs ────────────────────────┐
                        │  Forecast EDR (WAM · HARMONIE · DKSS)                    │
                        │  oceanObs (tide predictions · sea level · water temp)    │
                        │  metObs (coastal wind observations)                      │
                        └──────────────▲───────────────────────────────▲───────────┘
                                       │ poll 4×/day                   │ poll hourly
┌──────────────────────────────────────┴───────────────────────────────────────────┐
│ BACKEND (FastAPI, Python)                                                        │
│  ┌─────────────┐   ┌──────────────────┐   ┌────────────────────────────────┐    │
│  │ Ingestion   │ → │ Normalization &  │ → │ TimescaleDB/PostGIS            │    │
│  │ workers     │   │ QC pipeline      │   │  · spots · forecasts (hourly)  │    │
│  └─────────────┘   └──────────────────┘   │  · observations · tides        │    │
│                                           └────────────────────────────────┘    │
│  ┌───────────────────────────────────────┐   ┌───────────────────────────────┐  │
│  │ Surf rating engine (per spot, hourly):│   │ Public REST API (OpenAPI)     │  │
│  │ f(swell hgt/period/dir vs spot window,│   │  /spots /forecast /tides      │  │
│  │  wind speed/dir vs offshore sector,   │   │  /observations /alerts        │  │
│  │  tide window) → 0–5 stars + summary   │   └───────────────▲───────────────┘  │
│  └───────────────────────────────────────┘                   │                  │
│  Auth (favorites, alerts) · Push dispatcher (FCM → Android, APNs → iOS)         │
└──────────────────────────────────────────────────────────────┼──────────────────┘
                                                               │ HTTPS/JSON
            ┌──────────────────────┬───────────────────────────┴───┐
            │ Web (Next.js + TS)   │ Android (Flutter)             │ iOS (Flutter)
            │ PWA, SEO, MapLibre,  │ shared codebase, charts,      │ shared codebase,
            │ ECharts, favorites   │ offline cache, push           │ push via APNs
            └──────────────────────┴───────────────────────────────┘
```

**Key decisions**

- **One shared backend API** serves all three clients; mobile and web never call DMI directly (keeps DMI load tiny, lets us cache, and lets the rating engine evolve server-side).
- **Flutter** for Android + iOS from a single codebase (excellent charting, good offline cache, one team). Alternative: React Native if the team is JS-only — the API contract is identical either way.
- **PostgreSQL + TimescaleDB + PostGIS**: time-series forecasts, tide series and geo spot queries in one store. Redis for hot caching of "current conditions".
- **EU hosting** (e.g. Hetzner / Scaleway / AWS eu-north-1, the same region as DMI's open-data S3) — GDPR-friendly and low latency.

---

## 4. Backend design

### 4.1 Modules
1. **Ingestion workers** (cron containers): per spot, query EDR `position` endpoint for WAM (`wam_dw`, fallback `wam_nsb`), HARMONIE surface wind, DKSS water level; fetch tide predictions for the spot's tide station; fetch nearest metObs/oceanObs observations. Idempotent upserts keyed by (spot, model_run, valid_time).
2. **Normalization/QC**: units → metric (m, s, °, m/s), direction → "coming-from", gap detection, staleness flags, graceful fallback when a model run is late (keep previous run, mark `stale: true`).
3. **Rating engine**: pure function `(spot_profile, forecast_hour) → {stars 0–5, label, summary_da/en}`. Tunable per spot; versioned so we can A/B against user feedback.
4. **Public API**: REST + OpenAPI docs, API-key auth for third parties later, free tier for our own clients.
5. **Auth & profiles**: Supabase Auth or Firebase Auth; favorites, alert rules (spot + min stars + time window).
6. **Push dispatcher**: evaluates alert rules after each ingestion cycle; FCM for Android, APNs for iOS.

### 4.2 Data model (core tables)
```
spots(id, name_da, name_en, geom, facing_deg, swell_window, offshore_sector,
      tide_station_id, obs_station_id, best_tide, break_type, level, description)
forecast_runs(id, model, run_time, ingested_at)
forecast_hours(spot_id, run_id, valid_time, hs_m, tp_s, dir_deg, wind_ms, wind_dir,
               gust_ms, water_level_cm, air_temp_c, rating_stars, rating_label)
tides(station_id, time, level_cm, type)          -- from tidewater collection
observations(station_id, time, param, value)     -- metObs wind, oceanObs sea level/temp
users / favorites / alert_rules / push_tokens
```

### 4.3 API sketch
```
GET  /v1/spots?bbox=…                      → map pins with current rating
GET  /v1/spots/{id}                        → spot profile + current conditions
GET  /v1/spots/{id}/forecast?days=7        → hourly series: waves, wind, tide, rating
GET  /v1/spots/{id}/observations           → latest station readings
GET  /v1/regions                           → grouped spot lists (Vestkysten, Kattegat, Bornholm)
POST /v1/users/me/favorites                ← auth
POST /v1/users/me/alerts                   ← auth (spot, min_stars, window)
```

---

## 5. Frontend design

### 5.1 Web (Next.js + TypeScript + Tailwind + MapLibre + ECharts)
- `/` — map of Denmark with color-coded spot pins + "best right now" strip
- `/spots/[slug]` — the money page: hero rating, wave/wind/tide chart stack, hourly table, tide curve, observations, spot guide
- `/regions/[slug]` — spot lists; SEO-friendly (SSR) pages in Danish + English
- PWA manifest so the web app is installable on mobile from day one

### 5.2 Mobile (Flutter, single codebase)
Screens: Map/spot list → Spot detail (chart stack, tide curve) → Favorites → Alerts → Settings.
Local cache of last fetched forecasts (offline reading at the beach), push deep-links into spot pages.
Shared design tokens with the web app (colors, star rating, typography) for a consistent brand.

---

## 6. Roadmap

| Phase | Scope | Duration | Exit criteria |
|---|---|---|---|
| **0 — Prototype** | Ingest WAM + HARMONIE + tidewater for 3 spots (Klitmøller, Hvide Sande, Dueodde); minimal web page showing hourly tables + one chart; collect local surfer feedback on rating realism | 2–3 weeks | Data pipeline proven; rating heuristic v0 tuned against real sessions |
| **1 — Web MVP** | Full backend (DB, rating engine, public API), web app with map + 15 spots + tide curves + observations, Danish/English | 6–8 weeks | Public beta on the web; daily active ingestion with <1% missed runs |
| **2 — Mobile** | Flutter app (Android + iOS), auth, favorites, push alerts; store submissions | 4–6 weeks | Both apps live; alert delivery < 15 min after model ingestion |
| **3 — Growth** | Webcams, user surf reports ("how was it?"), more spots (incl. windswell/kite spots), premium tier (16-day outlook via fallback models, no ads), swell-history analytics | ongoing | Retention + alert engagement metrics in place |

**Team:** 1 backend engineer, 1 frontend/Flutter engineer, ~0.5 designer, plus 1–2 Danish surfers as domain advisors/testers. Solo-dev variant: stretch phases ×1.5–2.

**Indicative costs:** infra €50–200/mo at MVP scale (DMI data is free); Apple Developer $99/yr; Google Play $25 one-time; domain + email ~€50/yr.

---

## 7. Compliance, risks, mitigations

- **DMI terms:** free use incl. redistribution with attribution ("Data from the Danish Meteorological Institute"); raw data may not be altered — our derived ratings are new derived products, which is permitted. Add attribution in footer/About and link to dmi.dk/friedata.
- **Fair use:** 500 req/5 s is ample — per-spot ingestion ≈ 15 spots × ~4 endpoints × 4 runs/day.
- **Model resolution risk:** WAM doesn't resolve surf-zone bathymetry → calibrate per-spot shadowing/exposure in Phase 0 using metObs/oceanObs observations and surfer feedback; be explicit in UI that ratings are guidance, not safety data.
- **Reliability risk:** DMI occasionally has outages/late runs → keep previous run, show `stale` badge, add Open-Meteo fallback toggle.
- **GDPR:** minimal PII (email + push token), EU hosting, standard privacy policy.
- **Liability:** standard marine-forecast disclaimer (not for navigation/safety-of-life).

---

## 8. Immediate next steps

1. Register/confirm DMI open-data API access; spike the three EDR queries for one spot.
2. Draft the 15-spot catalog with exposure metadata (needs one experienced Danish surfer).
3. Build the Phase 0 ingestion script + a minimal spot page to validate data quality end-to-end.
