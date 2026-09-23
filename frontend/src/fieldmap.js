/* ---------------------------------------------------------------------------
 * fieldmap.js — regional gridded forecast map
 *
 * Renders DMI WAM model fields (exported by backend/export_fields.py to
 * /data/fields.json) over the satellite base map:
 *   layer "Wave height"  → filled + line contours of Hs, wave-direction arrows
 *   layer "Wave period"  → filled + line contours of swell period, wave arrows
 *   layer "Wind speed"   → filled + line contours of 10 m wind, wind arrows
 *
 * Time is scrubbed with a slider or the play button (loops). Spot markers
 * stay on top and remain clickable (same behaviour as the spot map).
 *
 * The component is re-attached after every app re-render (the host element
 * is rebuilt), but all UI state and prerendered frames live at module level,
 * so animation, position, layer and caches survive spot switches.
 * ------------------------------------------------------------------------- */

import { contours } from 'd3-contour';

const TILE = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile';
const Z_MIN = 6, Z_MAX = 14;

const mercX = (lon, z) => ((lon + 180) / 360) * 2 ** z;
const mercY = (lat, z) => {
  const rad = (lat * Math.PI) / 180;
  return ((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * 2 ** z;
};
const invMercX = (x, z) => (x / 2 ** z) * 360 - 180;
const invMercY = (y, z) => {
  const n = Math.PI * (1 - (2 * y) / 2 ** z);
  return (Math.atan(Math.sinh(n)) * 180) / Math.PI;
};

/* ---------------------------------------------------------- layer config */

const LAYERS = [
  { id: 'swh',  label: 'Wave height', dirField: 'mdir', magField: 'swh',  arrowCls: 'wave', minArrow: 0.2 },
  { id: 'mpts', label: 'Wave period', dirField: 'mdir', magField: 'swh',  arrowCls: 'wave', minArrow: 0.2 },
  { id: 'wind', label: 'Wind speed',  dirField: 'wdir', magField: 'wind', arrowCls: 'wind', minArrow: 0.5 },
];

const SCALES = {
  swh: {
    unit: 'm', maxLabel: '3+ m',
    stops: [[0, '#0d2c54'], [0.5, '#1f6f8b'], [1, '#2a9d8f'], [1.5, '#e9c46a'],
            [2, '#f4a261'], [2.5, '#e76f51'], [3, '#d62828']],
    th: [0.25, 0.5, 0.75, 1, 1.5, 2, 2.5, 3],
  },
  mpts: {
    unit: 's', maxLabel: '12+ s',
    stops: [[0, '#3b2f63'], [4, '#4361ee'], [6, '#4cc9f0'], [8, '#80ed99'],
            [10, '#ffd166'], [12, '#f4845f']],
    th: [4, 6, 8, 10, 12],
  },
  wind: {
    unit: 'm/s', maxLabel: '20+ m/s',
    stops: [[0, '#12335e'], [3, '#2a6f97'], [6, '#3d9df0'], [10, '#7b2cbf'],
            [15, '#c9184a'], [20, '#ff4d6d']],
    th: [2.5, 5, 7.5, 10, 12.5, 15, 20],
  },
};

const hexRgb = (h) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];

function colorFor(scale, v) {
  const st = scale.stops;
  if (v <= st[0][0]) return st[0][1];
  for (let k = 1; k < st.length; k++) {
    if (v <= st[k][0]) {
      const v0 = st[k - 1][0], c0 = hexRgb(st[k - 1][1]);
      const v1 = st[k][0], c1 = hexRgb(st[k][1]);
      const t = (v - v0) / (v1 - v0);
      const c = c0.map((a, i) => Math.round(a + (c1[i] - a) * t));
      return `rgb(${c[0]},${c[1]},${c[2]})`;
    }
  }
  return st[st.length - 1][1];
}

/* ------------------------------------------------------------ module state */

let C = null;          // context passed by main.js { fields, spots, activeSpot, coast, onSelect }
let S = null;          // ui state { layer, frame, playing, view }
let R = null;          // live DOM refs, refreshed on every attach
let hitSpots = [];
let lastActive = null; // last seen activeSpot — a change triggers spot-focus zoom
const pre = new Map(); // prerender cache "layer|frame" -> { canvas, contours }
let timer = 0;

const SPOT_ZOOM = 10;  // integer tile zoom for spot focus (~75 km across)

const layerDef = () => LAYERS.find((l) => l.id === S.layer);
const frames = () => C.fields.frames;
const grid = () => C.fields.grid;

function fitRegion(w, h) {
  const g = grid();
  const minLon = g.lon0, maxLon = g.lon0 + g.nx * g.dlon;
  const minLat = g.lat0, maxLat = g.lat0 + g.ny * g.dlat;
  const cLon = (minLon + maxLon) / 2, cLat = (minLat + maxLat) / 2;
  for (let z = 12; z >= Z_MIN; z--) {
    const zw = (mercX(maxLon, z) - mercX(minLon, z)) * 256;
    const zh = (mercY(minLat, z) - mercY(maxLat, z)) * 256;
    if (zw <= w * 0.92 && zh <= h * 0.92) return { lat: cLat, lon: cLon, z };
  }
  return { lat: cLat, lon: cLon, z: Z_MIN };
}

/* ------------------------------------------------------- prerender (cache) */

function prerender(fi) {
  const key = `${S.layer}|${fi}`;
  let p = pre.get(key);
  if (p) return p;
  const g = grid(), scale = SCALES[S.layer], fr = frames()[fi];
  const { nx, ny } = g;

  // filled field: one pixel per grid cell (row 0 = south → bottom of canvas)
  const canvas = document.createElement('canvas');
  canvas.width = nx; canvas.height = ny;
  const ctx = canvas.getContext('2d');
  const vals = new Array(nx * ny);
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const v = fr[S.layer][j * nx + i];
      if (v == null) { vals[j * nx + i] = -9999; continue; }
      vals[j * nx + i] = v;
      ctx.fillStyle = colorFor(scale, v);
      ctx.fillRect(i, ny - 1 - j, 1, 1);
    }
  }

  // contour lines (marching squares; -9999 holes = land)
  let cs = [];
  try {
    cs = contours().size([nx, ny]).thresholds(scale.th)(vals);
  } catch (e) { cs = []; }

  // Drop contour polygons that merely trace the land-mask edge: the lowest
  // thresholds hug the (blocky, ~5x9 km) model shoreline and look like a
  // coarse grey coastline on top of the satellite imagery. A polygon whose
  // outer ring runs mostly within 1 cell of land is an artifact, not data.
  const nearLand = (gx, gy) => {
    const ci = Math.round(gx), cj = Math.round(gy);
    for (let dj = -1; dj <= 1; dj++) {
      for (let di = -1; di <= 1; di++) {
        const j2 = cj + dj, i2 = ci + di;
        if (j2 >= 0 && j2 < ny && i2 >= 0 && i2 < nx && vals[j2 * nx + i2] === -9999) return true;
      }
    }
    return false;
  };
  cs.forEach((c) => {
    c.coordinates = c.coordinates.filter((poly) => {
      const ring = poly[0];
      if (!ring || !ring.length) return false;
      let edge = 0;
      for (const [gx, gy] of ring) if (nearLand(gx, gy)) edge++;
      return edge / ring.length < 0.35;
    });
  });

  p = { canvas, cs };
  pre.set(key, p);
  return p;
}

/* ---------------------------------------------------------------- drawing */

function project(lon, lat, w, h) {
  const st = S.view;
  const cx = mercX(st.lon, st.z) * 256, cy = mercY(st.lat, st.z) * 256;
  return [mercX(lon, st.z) * 256 - (cx - w / 2), mercY(lat, st.z) * 256 - (cy - h / 2)];
}

function draw() {
  if (!R || !S) return;
  const { el, tilesEl, canvas, svg } = R;
  const w = el.clientWidth || 900, h = el.clientHeight || 460;
  const g = grid(), st = S.view;

  /* satellite tiles (same approach as the spot map) */
  const cxp = mercX(st.lon, st.z) * 256, cyp = mercY(st.lat, st.z) * 256;
  const left = cxp - w / 2, top = cyp - h / 2;
  const n = 2 ** st.z;
  const tx0 = Math.max(0, Math.floor(left / 256));
  const tx1 = Math.min(n - 1, Math.floor((left + w - 1) / 256));
  const ty0 = Math.max(0, Math.floor(top / 256));
  const ty1 = Math.min(n - 1, Math.floor((top + h - 1) / 256));
  const want = new Set();
  for (let tx = tx0; tx <= tx1; tx++)
    for (let ty = ty0; ty <= ty1; ty++) want.add(`${tx}/${ty}`);
  tilesEl.querySelectorAll('img').forEach((img) => { if (!want.has(img.dataset.key)) img.remove(); });
  want.forEach((key) => {
    if (tilesEl.querySelector(`img[data-key="${key}"]`)) return;
    const [tx, ty] = key.split('/').map(Number);
    const img = document.createElement('img');
    img.className = 'tile'; img.dataset.key = key; img.draggable = false;
    img.src = `${TILE}/${st.z}/${ty}/${tx}`;
    img.onerror = () => { el.classList.add('no-tiles'); };
    tilesEl.appendChild(img);
  });
  tilesEl.querySelectorAll('img').forEach((img) => {
    const [tx, ty] = img.dataset.key.split('/').map(Number);
    img.style.left = `${tx * 256 - left}px`;
    img.style.top = `${ty * 256 - top}px`;
  });

  /* filled field overlay */
  canvas.width = w; canvas.height = h;
  const ctx2 = canvas.getContext('2d');
  const p = prerender(S.frame);
  const [fx0, fy1] = project(g.lon0, g.lat0, w, h);
  const [fx1, fy0] = project(g.lon0 + g.nx * g.dlon, g.lat0 + g.ny * g.dlat, w, h);
  ctx2.imageSmoothingEnabled = true;
  ctx2.globalAlpha = 0.62;
  ctx2.drawImage(p.canvas, fx0, fy0, fx1 - fx0, fy1 - fy0);
  ctx2.globalAlpha = 1;

  /* contour lines */
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.setAttribute('width', w); svg.setAttribute('height', h);
  let contourSvg = '';
  p.cs.forEach((c) => {
    let d = '';
    c.coordinates.forEach((poly) => {
      poly.forEach((ring) => {
        ring.forEach(([gx, gy], k) => {
          const [px, py] = project(g.lon0 + gx * g.dlon, g.lat0 + gy * g.dlat, w, h);
          d += `${k ? 'L' : 'M'}${px.toFixed(1)},${py.toFixed(1)}`;
        });
        d += 'Z';
      });
    });
    contourSvg += `<path class="fm-contour" d="${d}"/>`;
  });

  /* direction arrows on a coarse lattice (drawn TO where waves/wind go) */
  const ld = layerDef(), fr = frames()[S.frame];
  const step = 3;
  let arrows = '';
  for (let j = 1; j < g.ny - 1; j += step) {
    for (let i = 1; i < g.nx - 1; i += step) {
      const idx = j * g.nx + i;
      const dir = fr[ld.dirField][idx];
      const mag = fr[ld.magField][idx];
      if (dir == null || mag == null || mag < ld.minArrow) continue;
      const [x, y] = project(g.lon0 + (i + 0.5) * g.dlon, g.lat0 + (j + 0.5) * g.dlat, w, h);
      if (x < -20 || x > w + 20 || y < -20 || y > h + 20) continue;
      const to = ((dir + 180) % 360) * (Math.PI / 180);   // FROM → TO
      const ux = Math.sin(to), uy = -Math.cos(to);
      const tx = x + ux * 10, ty = y + uy * 10;
      const bx = x - ux * 5, by = y - uy * 5;
      const wx = -uy, wy = ux;
      const h1x = tx - ux * 4.5 + wx * 3, h1y = ty - uy * 4.5 + wy * 3;
      const h2x = tx - ux * 4.5 - wx * 3, h2y = ty - uy * 4.5 - wy * 3;
      arrows += `<path class="fm-arrow ${ld.arrowCls}" d="M${bx.toFixed(1)},${by.toFixed(1)}L${tx.toFixed(1)},${ty.toFixed(1)}M${h1x.toFixed(1)},${h1y.toFixed(1)}L${tx.toFixed(1)},${ty.toFixed(1)}L${h2x.toFixed(1)},${h2y.toFixed(1)}"/>`;
    }
  }

  /* spot markers (clickable, same behaviour as the spot map) */
  hitSpots = [];
  const placed = [];
  const overlaps = (x, y, lw) =>
    placed.some((b) => x < b.x + b.w && x + lw > b.x && y < b.y + b.h && y + 11 > b.y);
  let spotsSvg = '';
  C.spots.forEach((sp, i) => {
    const [x, y] = project(sp.lon, sp.lat, w, h);
    if (x < -80 || x > w + 80 || y < -40 || y > h + 40) return;
    hitSpots.push({ i, x, y });
    const active = i === C.activeSpot;
    const lw = sp.name.length * 5.6 + 10;
    let ly = y - 9;
    if (!active && overlaps(x + 9, ly - 9, lw)) ly = y + 19;
    if (!active) placed.push({ x: x + 9, y: ly - 9, w: lw, h: 11 });
    spotsSvg += `<g class="spotmark${active ? ' active' : ''}">
      <circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${active ? 7 : 5}"/>
      <text x="${(x + 9).toFixed(1)}" y="${ly.toFixed(1)}">${sp.name}</text></g>`;
  });

  svg.innerHTML = `${contourSvg}${arrows}${spotsSvg}`;
}

/* ------------------------------------------------------------- controls */

function fmtFrameTime(iso) {
  const d = new Date(iso);
  const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const p2 = (v) => String(v).padStart(2, '0');
  return `${days[d.getDay()]} ${p2(d.getDate())}/${p2(d.getMonth() + 1)} ${p2(d.getHours())}:00`;
}

function syncControls() {
  if (!R) return;
  const scale = SCALES[S.layer];
  R.slider.max = frames().length - 1;
  R.slider.value = S.frame;
  R.timeEl.textContent = fmtFrameTime(frames()[S.frame].time);
  R.playBtn.textContent = S.playing ? '⏸' : '▶';
  R.playBtn.classList.toggle('active', S.playing);
  R.layerBtns.forEach((b) => b.classList.toggle('active', b.dataset.layer === S.layer));
  const grad = scale.stops.map(([v, c], k) =>
    `${c} ${Math.round((v / scale.stops[scale.stops.length - 1][0]) * 100)}%`).join(', ');
  R.legendEl.innerHTML = `
    <span class="fm-leg-label">0</span>
    <span class="fm-leg-bar" style="background:linear-gradient(90deg, ${grad})"></span>
    <span class="fm-leg-label">${scale.maxLabel}</span>
    <span class="fm-leg-note">arrows = direction ${S.layer === 'wind' ? 'the wind blows' : 'the waves travel'} toward · lines = contours</span>`;
}

function tick() {
  if (!S || !S.playing || !R) return;
  S.frame = (S.frame + 1) % frames().length;
  syncControls();
  draw();
}

function setPlaying(on) {
  S.playing = on;
  clearInterval(timer);
  if (on) timer = setInterval(tick, 450);
  syncControls();
}

/* ---------------------------------------------------------------- attach */

function build(el) {
  el.innerHTML = `
    <div class="tiles"></div>
    <canvas class="field"></canvas>
    <svg class="overlay"></svg>
    <div class="controls">
      <button data-z="1" title="zoom in">+</button>
      <button data-z="-1" title="zoom out">−</button>
      <button data-fit="1" title="fit region">⌂</button>
    </div>
    <div class="fm-legend"></div>
    <div class="fm-bar">
      <span class="fm-layers">${LAYERS.map((l) =>
        `<button data-layer="${l.id}">${l.label}</button>`).join('')}</span>
      <button class="fm-play" title="play / pause">▶</button>
      <input type="range" class="fm-slider" min="0" step="1" value="${S.frame}" />
      <span class="fm-time"></span>
    </div>`;
  R = {
    el,
    tilesEl: el.querySelector('.tiles'),
    canvas: el.querySelector('canvas.field'),
    svg: el.querySelector('.overlay'),
    legendEl: el.querySelector('.fm-legend'),
    playBtn: el.querySelector('.fm-play'),
    slider: el.querySelector('.fm-slider'),
    timeEl: el.querySelector('.fm-time'),
    layerBtns: [...el.querySelectorAll('[data-layer]')],
  };

  el.querySelectorAll('.controls button').forEach((b) => {
    b.onclick = () => {
      if (b.dataset.fit) {
        S.view = fitRegion(el.clientWidth || 900, el.clientHeight || 460);
      } else {
        S.view.z = Math.max(Z_MIN, Math.min(Z_MAX, S.view.z + (+b.dataset.z)));
      }
      draw();
    };
  });
  el.addEventListener('wheel', (e) => {
    if (e.target.closest('.fm-bar') || e.target.closest('.controls')) return;
    e.preventDefault();
    const nz = Math.max(Z_MIN, Math.min(Z_MAX, S.view.z + (e.deltaY < 0 ? 1 : -1)));
    if (nz !== S.view.z) { S.view.z = nz; draw(); }
  }, { passive: false });

  let panStart = null, moved = 0;
  el.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.controls') || e.target.closest('.fm-bar')) return;
    panStart = { x: e.clientX, y: e.clientY, lon: S.view.lon, lat: S.view.lat };
    moved = 0;
    el.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  el.addEventListener('pointermove', (e) => {
    if (!panStart) return;
    moved = Math.max(moved, Math.abs(e.clientX - panStart.x) + Math.abs(e.clientY - panStart.y));
    const gx = mercX(panStart.lon, S.view.z) * 256 - (e.clientX - panStart.x);
    const gy = mercY(panStart.lat, S.view.z) * 256 - (e.clientY - panStart.y);
    S.view.lon = invMercX(gx / 256, S.view.z);
    S.view.lat = Math.max(-80, Math.min(80, invMercY(gy / 256, S.view.z)));
    draw();
  });
  el.addEventListener('pointerup', (e) => {
    if (!panStart) return;
    panStart = null;
    if (moved >= 6) return;
    const rect = el.getBoundingClientRect();
    const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
    let best = null, bestD = 16;
    hitSpots.forEach((s) => {
      const d = Math.hypot(s.x - cx, s.y - cy);
      if (d < bestD) { bestD = d; best = s; }
    });
    if (best && best.i !== C.activeSpot) C.onSelect(best.i);
  });

  R.layerBtns.forEach((b) => {
    b.onclick = () => { S.layer = b.dataset.layer; syncControls(); draw(); };
  });
  R.playBtn.onclick = () => setPlaying(!S.playing);
  R.slider.oninput = () => {
    setPlaying(false);
    S.frame = +R.slider.value;
    syncControls();
    draw();
  };

  let resizeRaf = 0;
  new ResizeObserver(() => {
    cancelAnimationFrame(resizeRaf);
    resizeRaf = requestAnimationFrame(draw);
  }).observe(el);
}

/* ------------------------------------------------------------------ entry */

export function initFieldMap(ctx) {
  C = ctx;
  const el = document.getElementById('fieldmap');
  if (!el) return;
  if (!C.fields || !C.fields.frames || !C.fields.frames.length) {
    el.innerHTML = '<div class="fm-empty">Regional field data not available — run <code>backend/export_fields.py</code></div>';
    R = null;
    return;
  }
  if (!S) {
    const now = Date.now();
    let fi = 0, bd = Infinity;
    C.fields.frames.forEach((f, i) => {
      const d = Math.abs(Date.parse(f.time) - now);
      if (d < bd) { bd = d; fi = i; }
    });
    S = { layer: 'swh', frame: fi, playing: false, view: null };
  }
  if (!S.view) S.view = fitRegion(el.clientWidth || 900, el.clientHeight || 460);
  // Focus the map on the spot when the selection changes (map click or tab);
  // first load keeps the Denmark-wide overview. ⌂ button = back to overview.
  if (lastActive === null) {
    lastActive = C.activeSpot;
  } else if (C.activeSpot !== lastActive) {
    lastActive = C.activeSpot;
    const sp = C.spots[C.activeSpot];
    if (sp) S.view = { lat: sp.lat, lon: sp.lon, z: SPOT_ZOOM };
  }
  build(el);
  syncControls();
  draw();
}
