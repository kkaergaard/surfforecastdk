import './style.css';
import { initFieldMap } from './fieldmap.js';

/* ---------------------------------------------------------------- helpers */

const COMPASS = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
const cardinal = (d) =>
  d == null ? '–' : COMPASS[Math.round((((d % 360) + 360) % 360) / 22.5) % 16];

const RATING_COLORS = {
  Poor: '#64748b', 'Fair−': '#60a5fa', Fair: '#2dd4bf', Good: '#4ade80', Epic: '#fbbf24',
};

const stars = (r) => '★★★★★☆☆☆☆☆'.slice(5 - Math.round(r), 10 - Math.round(r));
const fmtTick = (iso) => {
  const d = new Date(iso);
  return `${d.toLocaleDateString('en-GB', { weekday: 'short' })} ${String(d.getHours()).padStart(2, '0')}`;
};
const fmtHM = (iso) =>
  new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
const fmtFull = (iso) =>
  new Date(iso).toLocaleString('en-GB', {
    weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
const num = (v, nd = 1, unit = '') => (v == null ? '–' : `${v.toFixed(nd)}${unit}`);

function inSector(deg, a, b) {
  deg = ((deg % 360) + 360) % 360; a = ((a % 360) + 360) % 360; b = ((b % 360) + 360) % 360;
  return a <= b ? deg >= a && deg <= b : deg >= a || deg <= b;
}
const angDiff = (a, b) => Math.abs((((a - b + 180) % 360) + 360) % 360 - 180);

function windContext(spot, windDir) {
  if (windDir == null) return '';
  if (inSector(windDir, spot.offshore_sector[0], spot.offshore_sector[1])) return 'offshore';
  if (angDiff(windDir, spot.facing_deg) <= 45) return 'onshore';
  return 'cross-shore';
}

/* ------------------------------------------------------------------ state */

const app = document.getElementById('app');
let DATA = null;
let FIELDS = null;         // fields.json (regional gridded WAM fields for the map)
let LR = null;             // longrange.json (ECMWF/GFS 16-day guidance) — optional
let activeSpot = 0;

// Shared x-axis state: one time window for ALL charts, kept across spot tabs.
const domain = { start: 0, end: 0 };   // full forecast range (ms)
const view = { start: 0, end: 0 };     // current zoom window (ms)

// Independent x-axis state for the long-range section (16 days, own zoom).
const lrDomain = { start: 0, end: 0 };
const lrView = { start: 0, end: 0 };

init();

async function init() {
  try {
    const [fRes, fldRes, lrRes] = await Promise.all([
      fetch('data/forecast.json', { cache: 'no-store' }),
      fetch('data/fields.json', { cache: 'no-store' }).catch(() => null),
      fetch('data/longrange.json', { cache: 'no-store' }).catch(() => null),
    ]);
    if (!fRes.ok) throw new Error(`HTTP ${fRes.status}`);
    DATA = await fRes.json();
    if (fldRes && fldRes.ok) FIELDS = await fldRes.json();
    if (lrRes && lrRes.ok) LR = await lrRes.json().catch(() => null);
    computeDomain();
    resetView();
    computeLrDomain();
    lrResetView();
    render();
  } catch (e) {
    app.innerHTML = `<div class="error">
      Could not load <code>/data/forecast.json</code> (${e.message}).<br/>
      Run <code>python backend/ingest.py</code> from the workspace root first.
    </div>`;
  }
}

function computeDomain() {
  let s = Infinity, e = -Infinity;
  for (const sp of DATA.spots) {
    if (!sp.forecast || !sp.forecast.length) continue;
    s = Math.min(s, Date.parse(sp.forecast[0].time));
    e = Math.max(e, Date.parse(sp.forecast[sp.forecast.length - 1].time));
  }
  domain.start = s; domain.end = e;
}
function resetView() { view.start = domain.start; view.end = domain.end; }
function setView(s, e) {
  const MIN_SPAN = 3 * 3600e3;
  if (e - s < MIN_SPAN) e = s + MIN_SPAN;
  view.start = Math.max(domain.start, s);
  view.end = Math.min(domain.end, e);
  if (view.end - view.start < MIN_SPAN) view.start = view.end - MIN_SPAN;
}

function computeLrDomain() {
  if (!LR) return;
  let s = Infinity, e = -Infinity;
  for (const sp of LR.spots || []) {
    if (!sp.forecast || !sp.forecast.length) continue;
    s = Math.min(s, Date.parse(sp.forecast[0].time));
    e = Math.max(e, Date.parse(sp.forecast[sp.forecast.length - 1].time));
  }
  if (s < e) { lrDomain.start = s; lrDomain.end = e; }
}
function lrResetView() { lrView.start = lrDomain.start; lrView.end = lrDomain.end; }
function lrSetView(s, e) {
  const MIN_SPAN = 24 * 3600e3;
  if (e - s < MIN_SPAN) e = s + MIN_SPAN;
  lrView.start = Math.max(lrDomain.start, s);
  lrView.end = Math.min(lrDomain.end, e);
  if (lrView.end - lrView.start < MIN_SPAN) lrView.start = lrView.end - MIN_SPAN;
}

/* ---------------------------------------------------- shared time mapping */

const CW = 780;               // chart width (viewBox) — identical everywhere
const PL = 40, PR = 30;       // left/right padding — identical for ALL charts
const IW = CW - PL - PR;      // inner plot width
const xFor = (t) => PL + ((t - view.start) / (view.end - view.start)) * IW;
const tFor = (x) => view.start + ((x - PL) / IW) * (view.end - view.start);

const inView = (t) => t >= view.start && t <= view.end;
const spanHours = () => (view.end - view.start) / 3600e3;

function xTicks() {
  const h = spanHours();
  const step = h <= 30 ? 6 : h <= 84 ? 12 : 24;
  const ticks = [];
  const first = Math.ceil(view.start / (step * 3600e3)) * step * 3600e3;
  for (let t = first; t <= view.end; t += step * 3600e3) ticks.push(t);
  return ticks;
}
const xLabels = (H, pb) =>
  xTicks().map((t) =>
    `<text class="xlab" x="${xFor(t).toFixed(1)}" y="${H - pb + 18}" text-anchor="middle">${fmtTick(new Date(t).toISOString())}</text>`
  ).join('');

const arrowStep = () => Math.max(1, Math.round(spanHours() / 18));

/* --------------------------------------- long-range section: time mapping */

const lrXFor = (t) => PL + ((t - lrView.start) / (lrView.end - lrView.start)) * IW;
const lrTFor = (x) => lrView.start + ((x - PL) / IW) * (lrView.end - lrView.start);
const lrInView = (t) => t >= lrView.start && t <= lrView.end;
const lrSpanHours = () => (lrView.end - lrView.start) / 3600e3;

const fmtDay = (t) =>
  new Date(t).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric' });

// vertical gridlines at local midnights + date labels (reused by all LR charts)
function lrDayGrid(H, pt, pb) {
  let out = '';
  const d = new Date(lrView.start);
  d.setHours(0, 0, 0, 0);
  if (d.getTime() < lrView.start) d.setDate(d.getDate() + 1);
  for (let t = d.getTime(); t <= lrView.end;) {
    const x = lrXFor(t).toFixed(1);
    out += `<line class="grid" x1="${x}" x2="${x}" y1="${pt}" y2="${H - pb}"/>
      <text class="xlab" x="${x}" y="${H - pb + 18}" text-anchor="middle">${fmtDay(t)}</text>`;
    const nd = new Date(t);
    nd.setDate(nd.getDate() + 1);
    t = nd.getTime();
  }
  return out;
}

/* ----------------------------------------------------------------- render */

function chip(status) {
  return `<span class="chip ${status}">${status}</span>`;
}

function zoombarHtml() {
  const presets = [[24, '24 h'], [48, '48 h'], [72, '72 h'], [null, 'All']];
  const activeH = Math.round(spanHours());
  const btns = presets.map(([h, label]) => {
    const isActive = h == null
      ? view.start === domain.start && view.end === domain.end
      : Math.abs(spanHours() - h) < 1 && view.start === domain.start;
    return `<button data-preset="${h ?? 'all'}" class="${isActive ? 'active' : ''}">${label}</button>`;
  }).join('');
  return `<div class="zoombar">
    <span class="zb-label">Zoom:</span>${btns}
    <span class="zb-hint">drag across any chart to zoom · double-click to reset</span>
    <span class="zb-range">${fmtTick(new Date(view.start).toISOString())} → ${fmtTick(new Date(view.end).toISOString())} (${activeH} h)</span>
  </div>`;
}

function render() {
  const s = DATA.sources || {};
  const notes = (DATA.notes || []).length
    ? `<div class="notes">${DATA.notes.map((n) => `<div>⚠ ${n}</div>`).join('')}</div>`
    : '';
  app.innerHTML = `
    <header class="top">
      <h1><span class="wave">〜</span> Denmark Surf Forecast</h1>
      <span class="gen">generated ${fmtFull(DATA.generated_at)}</span>
    </header>
    <div class="src-chips">
      WAM ${chip(s.wam || 'none')} HARMONIE ${chip(s.harmonie || 'none')} Tides ${chip(s.tides || 'none')}
    </div>
    ${notes}
    <nav class="tabs">
      ${DATA.spots.map((sp, i) =>
        `<button class="${i === activeSpot ? 'active' : ''}" data-i="${i}">${sp.name}</button>`).join('')}
    </nav>
    <div class="card"><h2>Regional forecast map — DMI WAM model fields · drag to pan, scroll to zoom · click a spot to zoom to it · ⌂ full view</h2>
      <div class="satmap fieldmap" id="fieldmap"></div></div>
    ${zoombarHtml()}
    <main>${renderSpot(DATA.spots[activeSpot])}</main>
    <footer>Data: <a href="https://www.dmi.dk/fri-data" target="_blank" rel="noopener">DMI</a> WAM waves · DMI HARMONIE-DINI hi-res wind via <a href="https://open-meteo.com/" target="_blank" rel="noopener">Open-Meteo</a> · DMI OceanObs tide predictions ·
      long range: ECMWF IFS/WAM &amp; NOAA GFS/GFS-Wave via Open-Meteo ·
      ratings are a local heuristic, not an official DMI product</footer>`;

  app.querySelectorAll('nav.tabs button').forEach((b) => {
    b.onclick = () => { activeSpot = +b.dataset.i; render(); };
  });
  app.querySelectorAll('.zoombar button[data-preset]').forEach((b) => {
    b.onclick = () => {
      const p = b.dataset.preset;
      if (p === 'all') resetView();
      else setView(domain.start, domain.start + (+p) * 3600e3);
      render();
    };
  });
  app.querySelectorAll('.lr-zoombar button[data-lrpreset]').forEach((b) => {
    b.onclick = () => {
      const p = b.dataset.lrpreset;
      if (p === 'all') lrResetView();
      else lrSetView(lrDomain.start, lrDomain.start + (+p) * 3600e3);
      render();
    };
  });
  app.querySelectorAll('svg.chart').forEach(attachZoom);
  app.querySelectorAll('svg.lrchart').forEach(attachLrZoom);
  initFieldMap({
    fields: FIELDS,
    spots: DATA.spots,
    activeSpot,
    onSelect: (i) => { activeSpot = i; render(); },
  });
}

function renderSpot(spot) {
  return `
    ${heroCard(spot)}
    <div class="card"><h2>Swell — height (m), period (s) &amp; direction</h2>${waveChart(spot)}
      <div class="legend">
        <span><span class="sw" style="background:var(--blue)"></span>swell height</span>
        <span><span class="sw" style="background:var(--gold)"></span>mean period</span>
        <span><span class="sw dashed" style="background:#f4a261"></span>peak period (Tp — drives the rating)</span>
        <span>top arrows = direction the swell travels toward</span>
      </div></div>
    <div class="card"><h2>Wind — speed / gust (m/s) &amp; direction</h2>${windChart(spot)}
      <div class="legend">
        <span><span class="sw" style="background:var(--accent)"></span>wind (HARMONIE)</span>
        <span><span class="sw dashed" style="background:var(--accent)"></span>wind beyond ~2.5 days (WAM, lower res)</span>
        <span><span class="sw" style="background:var(--pink)"></span>gust</span>
        <span>arrow = direction wind blows toward</span>
      </div></div>
    <div class="card"><h2>Tide — predicted level (cm DVR90) · station ${spot.tide_station}</h2>
      ${tideChart(spot)}</div>
    <div class="card"><h2>Hourly — current zoom window</h2><div class="table-wrap">${hourTable(spot)}</div>
      <div class="legend-note">wind markers: <b style="color:#4ade80">✓</b> favorable (epic possible) ·
        <b style="color:#fbbf24">~</b> moderate (max Good) · <b style="color:#fb7185">✗</b> poor/bad (max Fair or lower) —
        hover for the rule · ↓ = rating capped (hover for reason) ·
        * = wind from the WAM wave model (extended range, lower resolution)</div></div>
    ${lrSection(spot)}`;
}

function heroCard(spot) {
  const now = Date.now();
  const cur =
    spot.forecast.filter((r) => Date.parse(r.time) <= now).pop() || spot.forecast[0] || {};
  const color = RATING_COLORS[cur.rating_label] || '#94a3b8';
  const nextHl = (spot.high_low || []).filter((e) => Date.parse(e.time) > now);
  const nextHigh = nextHl.find((e) => e.type === 'high');
  const nextLow = nextHl.find((e) => e.type === 'low');
  const ctx = windContext(spot, cur.wind_dir);
  const m = spot.meta || {};
  const metaBits = [];
  if (m.wam) metaBits.push(`WAM ${m.wam.collection} · ${m.wam.steps} steps · to ${m.wam.last_step ? fmtTick(m.wam.last_step) : '–'}`);
  if (m.harmonie) metaBits.push(`HARMONIE ${m.harmonie.steps} steps`);
  const capNote = cur.rating_capped
    ? `<div class="spot-sub cap">↓ capped: ${cur.rating_note}</div>` : '';
  return `
    <div class="card hero">
      <div class="rating">
        <div class="stars">${stars(cur.rating ?? 0)}</div>
        <div class="rating-label" style="color:${color}">${cur.rating_label || '–'}
          <span style="font-size:13px;color:var(--muted)">${num(cur.rating, 1)}/5</span></div>
        <div class="spot-sub">${spot.region} · faces ${cardinal(spot.facing_deg)} (${spot.facing_deg}°)</div>
        ${capNote}
        <div class="spot-sub">spot data: ${chip(spot.status.wam)} ${chip(spot.status.harmonie)} ${chip(spot.status.tides)}</div>
      </div>
      <div class="nums">
        <div class="num"><div class="k">Swell</div>
          <div class="v">${num(cur.swell_h ?? cur.hs, 1)} m</div>
          <div class="s">${num(cur.swell_period, 1)} s · ${cardinal(cur.swell_dir)} (${cur.swell_dir ?? '–'}°)</div></div>
        <div class="num"><div class="k">Total wave Hs</div>
          <div class="v">${num(cur.hs, 1)} m</div>
          <div class="s">windwave ${num(cur.windwave_h, 1)} m</div></div>
        <div class="num"><div class="k">Wind</div>
          <div class="v">${num(cur.wind_ms, 1)} m/s</div>
          <div class="s">${cardinal(cur.wind_dir)} (${cur.wind_dir ?? '–'}°) ${ctx}</div></div>
        <div class="num"><div class="k">Gust</div>
          <div class="v">${num(cur.gust_ms, 1)} m/s</div>
          <div class="s">air ${num(cur.air_temp_c, 1)} °C</div></div>
        <div class="num"><div class="k">Next high</div>
          <div class="v">${nextHigh ? fmtHM(nextHigh.time) : '–'}</div>
          <div class="s">${nextHigh ? `${num(nextHigh.level_cm, 0)} cm · ${fmtTick(nextHigh.time)}` : ''}</div></div>
        <div class="num"><div class="k">Next low</div>
          <div class="v">${nextLow ? fmtHM(nextLow.time) : '–'}</div>
          <div class="s">${nextLow ? `${num(nextLow.level_cm, 0)} cm · ${fmtTick(nextLow.time)}` : ''}</div></div>
      </div>
    </div>
    <div class="meta-line">${metaBits.join(' · ')}</div>`;
}

/* ----------------------------------------------------------------- charts */

function rowsInView(rows) {
  return rows.filter((r) => inView(Date.parse(r.time)));
}

function waveChart(spot) {
  const rows = rowsInView(spot.forecast);
  const H = 200, pt = 26, pb = 26;   // extra top space for direction arrows
  const ih = H - pt - pb;
  const all = spot.forecast;
  const maxH = Math.max(0.6, ...all.map((r) => Math.max(r.swell_h ?? 0, r.hs ?? 0))) * 1.15;
  // dynamic period axis: 0 → 1.2 × max period in the current zoom window
  const pVals = rows.flatMap((r) => [r.swell_period, r.peak_period])
    .filter((v) => typeof v === 'number');
  const pMax = (pVals.length ? Math.max(...pVals) : 10) * 1.2;
  const hourPx = IW / Math.max(1, spanHours());
  const bw = Math.max(1, hourPx * 0.8);
  const yH = (v) => pt + ih - (v / maxH) * ih;
  const yP = (p) => pt + ih - (Math.min(Math.max(p, 0), pMax) / pMax) * ih;
  let bars = '', grid = '', arrows = '';
  const pts = [];
  const pk = [];
  const step = arrowStep();
  rows.forEach((r, i) => {
    const t = Date.parse(r.time);
    const h = r.swell_h ?? r.hs ?? 0;
    bars += `<rect class="bar" x="${(xFor(t) - bw / 2).toFixed(1)}" y="${yH(h).toFixed(1)}"
      width="${bw.toFixed(1)}" height="${Math.max(0, pt + ih - yH(h)).toFixed(1)}" rx="1.5"/>`;
    if (r.swell_period != null)
      pts.push(`${xFor(t).toFixed(1)},${yP(r.swell_period).toFixed(1)}`);
    if (r.peak_period != null)
      pk.push(`${xFor(t).toFixed(1)},${yP(r.peak_period).toFixed(1)}`);
    if (i % step === 0 && r.swell_dir != null) {
      const toDir = (r.swell_dir + 180) % 360;
      const ax = xFor(t), ay = pt - 8;
      arrows += `<g transform="translate(${ax.toFixed(1)},${ay.toFixed(1)}) rotate(${toDir})">
          <path class="sdir-arrow" d="M0,-6 L3,4 L0,2 L-3,4 Z"/></g>
        <text class="adir" x="${ax.toFixed(1)}" y="${(ay + 14).toFixed(1)}">${cardinal(r.swell_dir)}</text>`;
    }
  });
  for (let g = 0.5; g < maxH; g += 0.5)
    grid += `<line class="grid" x1="${PL}" x2="${CW - PR}" y1="${yH(g)}" y2="${yH(g)}"/>
      <text class="ylab" x="${PL - 5}" y="${yH(g) + 3}">${g.toFixed(1)}</text>`;
  // right-side period ticks follow the dynamic period axis
  const pStep = Math.max(1, Math.ceil(pMax / 4));
  for (let p = pStep; p < pMax; p += pStep)
    grid += `<text class="xlab" x="${CW - PR + 4}" y="${yP(p) + 3}">${p}s</text>`;
  return `<svg class="chart" viewBox="0 0 ${CW} ${H}" role="img" aria-label="swell chart">
    ${grid}${bars}<polyline class="period-line" points="${pts.join(' ')}"/>
    <polyline class="period-line peak" points="${pk.join(' ')}"/>${arrows}${xLabels(H, pb)}</svg>`;
}

function windChart(spot) {
  const rows = rowsInView(spot.forecast).filter((r) => r.wind_ms != null);
  const H = 190, pt = 16, pb = 26;
  const ih = H - pt - pb;
  const all = spot.forecast;
  const maxV = Math.max(5, ...all.map((r) => Math.max(r.wind_ms ?? 0, r.gust_ms ?? 0))) * 1.15;
  const y = (v) => pt + ih - (v / maxV) * ih;
  let grid = '', arrows = '';
  const gPts = [];
  const step = arrowStep();
  // wind line split into segments by source: HARMONIE (solid) vs WAM (dashed)
  const segs = [];
  let cur = null;
  rows.forEach((r, i) => {
    const t = Date.parse(r.time);
    const ax = xFor(t);
    const src = r.wind_src === 'wam' ? 'wam' : 'harmonie';
    if (!cur || cur.src !== src) {
      if (cur) segs.push(cur);
      cur = { src, pts: [] };
    }
    cur.pts.push(`${ax.toFixed(1)},${y(r.wind_ms).toFixed(1)}`);
    if (r.gust_ms != null) gPts.push(`${ax.toFixed(1)},${y(r.gust_ms).toFixed(1)}`);
    if (i % step === 0 && r.wind_dir != null) {
      const ay = y(r.wind_ms) - 12 < pt + 6 ? y(r.wind_ms) + 16 : y(r.wind_ms) - 12;
      const toDir = (r.wind_dir + 180) % 360;
      arrows += `<g transform="translate(${ax.toFixed(1)},${ay.toFixed(1)}) rotate(${toDir})">
          <path class="arrow" d="M0,-6 L3,4 L0,2 L-3,4 Z"/></g>
        <text class="adir" x="${ax.toFixed(1)}" y="${ay + 14}">${cardinal(r.wind_dir)}</text>`;
    }
  });
  if (cur) segs.push(cur);
  // stitch segments together visually: start each segment at the previous one's last point
  const lines = segs.map((s, idx) => {
    const pts = s.pts.filter(Boolean);
    if (idx > 0 && segs[idx - 1].pts.filter(Boolean).length) {
      const prevPts = segs[idx - 1].pts.filter(Boolean);
      pts.unshift(prevPts[prevPts.length - 1]);
    }
    const cls = s.src === 'wam' ? 'wind-line wam' : 'wind-line';
    return `<polyline class="${cls}" points="${pts.join(' ')}"/>`;
  }).join('');
  for (let g = 5; g < maxV; g += 5)
    grid += `<line class="grid" x1="${PL}" x2="${CW - PR}" y1="${y(g)}" y2="${y(g)}"/>
      <text class="ylab" x="${PL - 5}" y="${y(g) + 3}">${g}</text>`;
  return `<svg class="chart" viewBox="0 0 ${CW} ${H}" role="img" aria-label="wind chart">
    ${grid}<polyline class="gust-line" points="${gPts.join(' ')}"/>
    ${lines}${arrows}${xLabels(H, pb)}</svg>`;
}

function tideChart(spot) {
  const data = (spot.tides || []).filter((r) => inView(Date.parse(r.time)));
  if (!data.length) return '<div class="meta-line">no tide data in this window</div>';
  const H = 150, pt = 14, pb = 24;
  const ih = H - pt - pb;
  const vals = data.map((r) => r.level_cm);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = Math.max(5, (hi - lo) * 0.15);
  const yMin = lo - pad, yMax = hi + pad;
  const y = (v) => pt + ih - ((v - yMin) / (yMax - yMin)) * ih;
  const pts = data.map((r) => `${xFor(Date.parse(r.time)).toFixed(1)},${y(r.level_cm).toFixed(1)}`);
  const area = `${xFor(view.start)},${y(yMin)} ${pts.join(' ')} ${xFor(view.end)},${y(yMin)}`;
  // y-axis: nice-step gridlines + labels (cm)
  const yRange = yMax - yMin;
  const yStep = [5, 10, 20, 25, 50, 100].find((s) => s >= yRange / 4) || 200;
  let grid = '';
  for (let v = Math.ceil(yMin / yStep) * yStep; v <= yMax; v += yStep) {
    const gy = y(v);
    grid += `<line class="grid" x1="${PL}" x2="${CW - PR}" y1="${gy.toFixed(1)}" y2="${gy.toFixed(1)}"/>
      <text class="ylab" x="${PL - 5}" y="${(gy + 3).toFixed(1)}">${Math.round(v)}</text>`;
  }
  let marks = '';
  (spot.high_low || []).forEach((e) => {
    const t = Date.parse(e.time);
    if (!inView(t)) return;
    const cx = xFor(t), cy = y(e.level_cm);
    marks += `<circle class="${e.type === 'high' ? 'hl-high' : 'hl-low'}" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="3.5"/>
      <text class="hl-text" x="${cx.toFixed(1)}" y="${(e.type === 'high' ? cy - 7 : cy + 13).toFixed(1)}">
        ${e.type === 'high' ? 'H' : 'L'} ${fmtHM(e.time)}</text>`;
  });
  return `<svg class="chart" viewBox="0 0 ${CW} ${H}" role="img" aria-label="tide chart">
    ${grid}<polygon class="tide-fill" points="${area}"/>
    <polyline class="tide-line" points="${pts.join(' ')}"/>${marks}${xLabels(H, pb)}</svg>`;
}

/* ------------------------------------------------- long-range (ECMWF/GFS) */

function lrRowsInView(rows) {
  return rows.filter((r) => lrInView(Date.parse(r.time)));
}

// polyline segments, breaking the line wherever the value is null
function lrSegLines(rows, getV, y, cls) {
  let out = '', pts = [];
  const flush = () => {
    if (pts.length > 1) out += `<polyline class="${cls}" points="${pts.join(' ')}"/>`;
    pts = [];
  };
  for (const r of rows) {
    const v = getV(r);
    if (v == null) { flush(); continue; }
    pts.push(`${lrXFor(Date.parse(r.time)).toFixed(1)},${y(v).toFixed(1)}`);
  }
  flush();
  return out;
}

// translucent band between the two models (only where both have values)
function lrSpreadBand(rows, getA, getB, y) {
  let out = '', top = [], bot = [];
  const flush = () => {
    if (top.length > 1)
      out += `<polygon class="lr-band" points="${top.join(' ')} ${bot.reverse().join(' ')}"/>`;
    top = []; bot = [];
  };
  for (const r of rows) {
    const a = getA(r), b = getB(r);
    if (a == null || b == null) { flush(); continue; }
    const x = lrXFor(Date.parse(r.time)).toFixed(1);
    top.push(`${x},${y(Math.max(a, b)).toFixed(1)}`);
    bot.push(`${x},${y(Math.min(a, b)).toFixed(1)}`);
  }
  flush();
  return out;
}

function lrZoombarHtml() {
  const presets = [[72, '3 d'], [168, '7 d'], [null, '16 d']];
  const days = Math.round(lrSpanHours() / 24);
  const btns = presets.map(([h, label]) => {
    const isActive = h == null
      ? lrView.start === lrDomain.start && lrView.end === lrDomain.end
      : Math.abs(lrSpanHours() - h) < 1 && lrView.start === lrDomain.start;
    return `<button data-lrpreset="${h ?? 'all'}" class="${isActive ? 'active' : ''}">${label}</button>`;
  }).join('');
  return `<div class="zoombar lr-zoombar">
    <span class="zb-label">Zoom:</span>${btns}
    <span class="zb-hint">drag across any long-range chart to zoom · double-click to reset</span>
    <span class="zb-range">${fmtDay(lrView.start)} → ${fmtDay(lrView.end)} (${days} d)</span>
  </div>`;
}

function lrSection(spot) {
  if (!LR) return '';
  const lsp = (LR.spots || []).find((s) => s.id === spot.id);
  if (!lsp || !lsp.forecast || !lsp.forecast.length) return '';
  const rateLegend = Object.entries(RATING_COLORS).map(([k, v]) =>
    `<span><span class="rw" style="background:${v}"></span>${k}</span>`).join('');
  return `
    <div class="card lr">
      <h2>Long range · ECMWF vs GFS</h2>
      <div class="lr-note">${LR.note || ''}</div>
      <div class="lr-note">updated ${fmtFull(LR.generated_at)} · ${LR.source || ''}</div>
      ${lrZoombarHtml()}
      <h3>Wave height (m)</h3>${lrWaveChart(lsp)}
      <div class="legend">
        <span><span class="sw" style="background:var(--blue)"></span>ECMWF Hs</span>
        <span><span class="sw dash-blue"></span>GFS Hs</span>
        <span><span class="sw band-sw"></span>model spread (min–max)</span>
      </div>
      <h3>Mean period (s)</h3>${lrPeriodChart(lsp)}
      <div class="legend">
        <span><span class="sw" style="background:var(--gold)"></span>ECMWF mean period</span>
        <span><span class="sw dash-gold"></span>GFS mean period</span>
      </div>
      <h3>Wind (m/s) &amp; direction</h3>${lrWindChart(lsp)}
      <div class="legend">
        <span><span class="sw" style="background:var(--accent)"></span>ECMWF wind</span>
        <span><span class="sw dashed"></span>GFS wind</span>
        <span><span class="sw" style="background:var(--pink)"></span>ECMWF gust</span>
        <span>arrow = direction wind blows toward (ECMWF, GFS where ECMWF ends)</span>
      </div>
      <h3>Rating — ECMWF (top) vs GFS (bottom)</h3>${lrRatingChart(lsp)}
      <div class="legend">${rateLegend}</div>
    </div>`;
}

function lrWaveChart(lsp) {
  const rows = lrRowsInView(lsp.forecast);
  const H = 180, pt = 12, pb = 26;
  const ih = H - pt - pb;
  const all = lsp.forecast;
  const maxH = Math.max(0.6, ...all.map((r) =>
    Math.max((r.ecmwf || {}).hs ?? 0, (r.gfs || {}).hs ?? 0))) * 1.15;
  const y = (v) => pt + ih - (v / maxH) * ih;
  let grid = '';
  for (let g = 0.5; g < maxH; g += 0.5)
    grid += `<line class="grid" x1="${PL}" x2="${CW - PR}" y1="${y(g).toFixed(1)}" y2="${y(g).toFixed(1)}"/>
      <text class="ylab" x="${PL - 5}" y="${(y(g) + 3).toFixed(1)}">${g.toFixed(1)}</text>`;
  const band = lrSpreadBand(rows, (r) => (r.ecmwf || {}).hs, (r) => (r.gfs || {}).hs, y);
  const ecmwf = lrSegLines(rows, (r) => (r.ecmwf || {}).hs, y, 'lr-hs-ecmwf');
  const gfs = lrSegLines(rows, (r) => (r.gfs || {}).hs, y, 'lr-hs-gfs');
  return `<svg class="lrchart" viewBox="0 0 ${CW} ${H}" role="img" aria-label="long-range wave height chart">
    ${lrDayGrid(H, pt, pb)}${grid}${band}${ecmwf}${gfs}</svg>`;
}

function lrPeriodChart(lsp) {
  const rows = lrRowsInView(lsp.forecast);
  const H = 160, pt = 12, pb = 26;
  const ih = H - pt - pb;
  const pVals = rows.flatMap((r) => [(r.ecmwf || {}).tm_s, (r.gfs || {}).tm_s])
    .filter((v) => typeof v === 'number');
  const pMax = (pVals.length ? Math.max(...pVals) : 10) * 1.2;
  const y = (v) => pt + ih - (Math.min(Math.max(v, 0), pMax) / pMax) * ih;
  let grid = '';
  const pStep = Math.max(1, Math.ceil(pMax / 4));
  for (let p = pStep; p < pMax; p += pStep)
    grid += `<line class="grid" x1="${PL}" x2="${CW - PR}" y1="${y(p).toFixed(1)}" y2="${y(p).toFixed(1)}"/>
      <text class="ylab" x="${PL - 5}" y="${(y(p) + 3).toFixed(1)}">${p}s</text>`;
  const ecmwf = lrSegLines(rows, (r) => (r.ecmwf || {}).tm_s, y, 'lr-tm-ecmwf');
  const gfs = lrSegLines(rows, (r) => (r.gfs || {}).tm_s, y, 'lr-tm-gfs');
  return `<svg class="lrchart" viewBox="0 0 ${CW} ${H}" role="img" aria-label="long-range mean period chart">
    ${lrDayGrid(H, pt, pb)}${grid}${ecmwf}${gfs}</svg>`;
}

function lrWindChart(lsp) {
  const rows = lrRowsInView(lsp.forecast);
  const H = 180, pt = 16, pb = 26;
  const ih = H - pt - pb;
  const all = lsp.forecast;
  const maxV = Math.max(5, ...all.map((r) => Math.max(
    (r.ecmwf || {}).wind_ms ?? 0, (r.gfs || {}).wind_ms ?? 0,
    (r.ecmwf || {}).gust_ms ?? 0))) * 1.15;
  const y = (v) => pt + ih - (v / maxV) * ih;
  let grid = '', arrows = '';
  for (let g = 5; g < maxV; g += 5)
    grid += `<line class="grid" x1="${PL}" x2="${CW - PR}" y1="${y(g).toFixed(1)}" y2="${y(g).toFixed(1)}"/>
      <text class="ylab" x="${PL - 5}" y="${(y(g) + 3).toFixed(1)}">${g}</text>`;
  // one arrow per ~18 px; ECMWF direction where available, GFS beyond
  const step = Math.max(1, Math.round((rows.length * 18) / IW));
  rows.forEach((r, i) => {
    if (i % step !== 0) return;
    const em = r.ecmwf || {}, gf = r.gfs || {};
    const dir = em.wind_dir ?? gf.wind_dir;
    const v = em.wind_ms ?? gf.wind_ms;
    if (dir == null || v == null) return;
    const ax = lrXFor(Date.parse(r.time));
    const ay = y(v) - 12 < pt + 6 ? y(v) + 16 : y(v) - 12;
    const toDir = (dir + 180) % 360;
    arrows += `<g transform="translate(${ax.toFixed(1)},${ay.toFixed(1)}) rotate(${toDir})">
        <path class="arrow" d="M0,-6 L3,4 L0,2 L-3,4 Z"/></g>
      <text class="adir" x="${ax.toFixed(1)}" y="${(ay + 14).toFixed(1)}">${cardinal(dir)}</text>`;
  });
  const gust = lrSegLines(rows, (r) => (r.ecmwf || {}).gust_ms, y, 'lr-gust');
  const ecmwf = lrSegLines(rows, (r) => (r.ecmwf || {}).wind_ms, y, 'lr-wind-ecmwf');
  const gfs = lrSegLines(rows, (r) => (r.gfs || {}).wind_ms, y, 'lr-wind-gfs');
  return `<svg class="lrchart" viewBox="0 0 ${CW} ${H}" role="img" aria-label="long-range wind chart">
    ${lrDayGrid(H, pt, pb)}${grid}${gust}${ecmwf}${gfs}${arrows}</svg>`;
}

function lrRatingChart(lsp) {
  const rows = lrRowsInView(lsp.forecast);
  const H = 66, pt = 6, pb = 24;
  const rowH = 12, gap = 4;
  const yE = pt, yG = pt + rowH + gap;
  let blocks = '';
  rows.forEach((r, i) => {
    const t = Date.parse(r.time);
    const t2 = i + 1 < rows.length ? Date.parse(rows[i + 1].time) : t + 3 * 3600e3;
    const x = lrXFor(t);
    const w = Math.max(0.5, lrXFor(Math.min(t2, lrView.end)) - x - 0.6);
    const em = r.ecmwf || {}, gf = r.gfs || {};
    const ec = em.rating_label ? RATING_COLORS[em.rating_label] : null;
    const gc = gf.rating_label ? RATING_COLORS[gf.rating_label] : null;
    blocks += `<rect class="lr-rate" x="${x.toFixed(1)}" y="${yE}" width="${w.toFixed(1)}" height="${rowH}" rx="1.5"
        fill="${ec || '#26314a'}"><title>ECMWF · ${fmtFull(r.time)} — ${em.rating_label || 'no data'}</title></rect>
      <rect class="lr-rate" x="${x.toFixed(1)}" y="${yG}" width="${w.toFixed(1)}" height="${rowH}" rx="1.5"
        fill="${gc || '#26314a'}"><title>GFS · ${fmtFull(r.time)} — ${gf.rating_label || 'no data'}</title></rect>`;
  });
  return `<svg class="lrchart" viewBox="0 0 ${CW} ${H}" role="img" aria-label="long-range rating strip">
    ${lrDayGrid(H, pt, pb)}
    <text class="lr-mod" x="${PL - 5}" y="${yE + 9}">ECMWF</text>
    <text class="lr-mod" x="${PL - 5}" y="${yG + 9}">GFS</text>
    ${blocks}</svg>`;
}

/* ------------------------------------------------------------ zoom wiring */

function attachZoom(svg) {
  let dragStart = null, selRect = null;
  const svgX = (clientX) => {
    const r = svg.getBoundingClientRect();
    return Math.max(PL, Math.min(CW - PR, ((clientX - r.left) / r.width) * CW));
  };
  svg.addEventListener('pointerdown', (e) => {
    dragStart = svgX(e.clientX);
    selRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    selRect.setAttribute('class', 'sel-rect');
    selRect.setAttribute('y', '0');
    selRect.setAttribute('height', String(svg.viewBox.baseVal.height));
    svg.appendChild(selRect);
    svg.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  svg.addEventListener('pointermove', (e) => {
    if (dragStart == null || !selRect) return;
    const x1 = svgX(e.clientX);
    selRect.setAttribute('x', String(Math.min(dragStart, x1)));
    selRect.setAttribute('width', String(Math.abs(x1 - dragStart)));
  });
  svg.addEventListener('pointerup', (e) => {
    if (dragStart == null) return;
    const x1 = svgX(e.clientX);
    const w = Math.abs(x1 - dragStart);
    const startX = dragStart;
    dragStart = null; selRect = null;
    if (w > 8) {
      setView(tFor(Math.min(startX, x1)), tFor(Math.max(startX, x1)));
      render();
    }
  });
  svg.addEventListener('dblclick', () => { resetView(); render(); });
}

// same drag-to-zoom interaction, but driving the long-range section's own view
function attachLrZoom(svg) {
  let dragStart = null, selRect = null;
  const svgX = (clientX) => {
    const r = svg.getBoundingClientRect();
    return Math.max(PL, Math.min(CW - PR, ((clientX - r.left) / r.width) * CW));
  };
  svg.addEventListener('pointerdown', (e) => {
    dragStart = svgX(e.clientX);
    selRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    selRect.setAttribute('class', 'sel-rect');
    selRect.setAttribute('y', '0');
    selRect.setAttribute('height', String(svg.viewBox.baseVal.height));
    svg.appendChild(selRect);
    svg.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  svg.addEventListener('pointermove', (e) => {
    if (dragStart == null || !selRect) return;
    const x1 = svgX(e.clientX);
    selRect.setAttribute('x', String(Math.min(dragStart, x1)));
    selRect.setAttribute('width', String(Math.abs(x1 - dragStart)));
  });
  svg.addEventListener('pointerup', (e) => {
    if (dragStart == null) return;
    const x1 = svgX(e.clientX);
    const w = Math.abs(x1 - dragStart);
    const startX = dragStart;
    dragStart = null; selRect = null;
    if (w > 8) {
      lrSetView(lrTFor(Math.min(startX, x1)), lrTFor(Math.max(startX, x1)));
      render();
    }
  });
  svg.addEventListener('dblclick', () => { lrResetView(); render(); });
}

/* ------------------------------------------------------------------ table */

const WC_MARK = {
  favorable: ['✓', '#4ade80', 'favorable wind — light (<3 m/s), offshore or sheltered; epic possible'],
  moderate:  ['~', '#fbbf24', 'moderate wind — caps rating at Good (4.0)'],
  poor:      ['✗', '#fb7185', 'poor wind — onshore ≥5 m/s or ≥8 m/s cross-shore; caps at Fair (3.0)'],
  bad:       ['✗', '#ef4444', 'bad wind — onshore ≥8 m/s or ≥12 m/s; caps at 2.5'],
};

function hourTable(spot) {
  const rows = rowsInView(spot.forecast);
  const body = rows.map((r) => {
    const c = RATING_COLORS[r.rating_label] || '#94a3b8';
    const cap = r.rating_capped
      ? ` <span class="capmark" title="${r.rating_note || 'capped'}">↓</span>` : '';
    const wamMark = r.wind_src === 'wam'
      ? `<span class="wammark" title="wind from WAM wave model (extended range, lower resolution)">*</span>` : '';
    const wc = WC_MARK[r.wind_class];
    const wcMark = wc
      ? `<span class="wcmark" style="color:${wc[1]}" title="${wc[2]}">${wc[0]}</span> ` : '';
    return `<tr>
      <td>${fmtFull(r.time)}</td>
      <td>${num(r.swell_h ?? r.hs, 1)}</td>
      <td>${num(r.swell_period, 1)}</td>
      <td>${cardinal(r.swell_dir)}</td>
      <td>${wcMark}${num(r.wind_ms, 1)}${wamMark}</td>
      <td>${num(r.gust_ms, 1)}</td>
      <td>${cardinal(r.wind_dir)}</td>
      <td>${num(r.air_temp_c, 1)}</td>
      <td class="rate" style="color:${c}">${r.rating_label || '–'}${cap}</td>
    </tr>`;
  }).join('');
  return `<table>
    <thead><tr><th>Time</th><th>Swell m</th><th>Per s</th><th>Dir</th>
      <th>Wind</th><th>Gust</th><th>Wdir</th><th>°C</th><th>Rating</th></tr></thead>
    <tbody>${body}</tbody></table>`;
}


