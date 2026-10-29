// Frontend logic test for templates/rolling_profile_snippet.html
// Run from the project root:   node tests/rolling_profile_snippet.test.js
// Uses minimal DOM + Chart.js stubs — no browser needed.
const fs = require('fs');
const path = require('path');
const SNIPPET = path.join(__dirname, '..', 'templates', 'rolling_profile_snippet.html');
const SRC = fs.readFileSync(SNIPPET, 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
const els = {};
function el(id) { return els[id] ||= { id, style: {}, textContent: '', className: '', value: '',
  listeners: {}, addEventListener(t, f) { this.listeners[t] = f; }, getContext: () => ({}) }; }
function node() { return { children: [], style: {}, appendChild(d) { this.children.push(d); },
  set innerHTML(h) { this._html = h; }, get innerHTML() { return this._html; },
  querySelector: sel => el(sel.replace('#', '')) }; }
const page = { inserted: [], insertBefore(n) { this.inserted.push(n); } };
const gexGrid = { parentNode: page, nextSibling: null };
global.document = {
  getElementById: id => id === 'gex-grid' ? gexGrid : el(id),
  createElement: () => node(),
};
global.localStorage = { _s: {}, getItem(k) { return this._s[k] ?? null; }, setItem(k, v) { this._s[k] = String(v); } };
const charts = [];
global.Chart = function (ctx, cfg) { charts.push(this); this.data = cfg.data; this.options = cfg.options;
  this.plugins = cfg.plugins; this.update = () => {}; this.resetZoom = () => { this.resets = (this.resets || 0) + 1; }; };

const SPOT = 7777.98, G_7780 = 0.02, G_OTHER = 0.001;
function windowData(w) {
  const strikes = [], call = [], put = [], gamma = [];
  for (let k = 7400; k <= 8150; k += 5) {
    strikes.push(k); call.push(k === 7780 ? 120000 : 100); put.push(k === 7750 ? 70000 : 50);
    gamma.push(k === 7780 ? G_7780 : G_OTHER);
  }
  const session = w === 'session';
  return { symbol: 'SPX', window_sec: session ? 0 : Number(w), covered_sec: session ? 21600 : Number(w),
           partial: w === '1800', spot: SPOT, updated: '14:24:30', strikes, call, put, gamma };
}
const responses = [];
let empty = false, only = null;
global.fetch = async url => {
  responses.push(url);
  const keys = url.split('windows=')[1].split(',').filter(k => !only || only.includes(k));
  const windows = {};
  for (const k of keys) windows[k] = empty ? { symbol: 'SPX', spot: null, strikes: [], call: [], put: [], gamma: [] } : windowData(k);
  return { ok: true, json: async () => ({ windows }) };
};
let poll = null;
global.setInterval = f => { poll = f; return 0; };
eval(SRC);
const tick = () => new Promise(r => setTimeout(r, 20));
(async () => {
  await tick();
  const assert = require('assert');
  const COLS = [0, 1, 2, 3];
  const row = charts.slice(0, 4);
  const views = () => row.map(c => [c.options.scales.y.min, c.options.scales.y.max]);
  const allView = (lo, hi, msg) => views().forEach(v => assert.deepStrictEqual(v, [lo, hi], msg));
  const at = (c, ds, k) => c.data.datasets[ds].data.find(p => p.y === k).x;
  const gexOf = vol => vol * G_7780 * 100 * SPOT;

  // own section: label + full-width grid inserted after #gex-grid
  assert.strictEqual(page.inserted.length, 2, 'section label + grid inserted');
  assert.strictEqual(page.inserted[0].textContent, 'ROLLING PROFILE');
  assert.strictEqual(page.inserted[1].className, 'grid rp-grid');
  const panel = page.inserted[1].children[0];
  assert(panel.className.includes('gex-chart-div'), 'resizable like the GEX panels');
  const html = panel._html;

  // every chart has its own time frame (all choices) and its own Volume / Flow GEX switch
  const selectOf = (h, i) => h.split(`id="rp-win-${i}"`)[1].split('</select>')[0];
  for (const i of COLS) {
    const sel = selectOf(html, i);
    for (const l of ['1 min', '2 min', '3 min', '5 min', '10 min', '15 min', '30 min', '1 hr', 'Session']) assert(sel.includes(`>${l}<`), `chart ${i} offers ${l}`);
    assert(html.includes(`id="rp-mode-${i}-vol" class="active">Volume<`), `chart ${i} starts in Volume`);
    assert(html.includes(`id="rp-mode-${i}-gex">Flow GEX<`));
    assert(html.includes(`id="rp-canvas-${i}"`));
  }
  ['180', '900', '1800', 'session'].forEach((w, i) => assert(selectOf(html, i).includes(`value="${w}" selected`), `chart ${i} defaults to ${w}`));
  assert(html.includes('id="rp-range"') && html.includes('value="75"'), 'range slider defaults to ±75');

  // one request for all four windows; four charts on the same strikes
  assert.deepStrictEqual(responses, ['/api/rolling_profile/SPX?windows=180,900,1800,session']);
  assert.strictEqual(charts.length, 4);
  allView(7700, 7855, '±75 around spot on every chart');
  const ys = row[0].data.datasets[0].data.map(p => p.y);
  assert(Math.min(...ys) < 7700 && Math.max(...ys) > 7855, 'data wider than the view');
  assert(Math.min(...ys) >= SPOT - 350 && Math.max(...ys) <= SPOT + 350, 'data capped at ±350');
  assert.strictEqual(row[0].options.indexAxis, 'y');

  // volume totals cover the strikes in view; per-chart spans
  assert.strictEqual(el('rp-ctot-0').textContent, '123K');
  assert.strictEqual(el('rp-ptot-0').textContent, '72K');
  assert.strictEqual(el('rp-net-0').textContent, '', 'no net in volume mode');
  assert.strictEqual(el('rp-span-0').textContent, 'actual 3m00s');
  assert.strictEqual(el('rp-span-1').textContent, 'actual 15m00s');
  assert.strictEqual(el('rp-span-2').textContent, 'only 30m00s of history');
  assert(el('rp-span-2').className.includes('rp-warn'));
  assert.strictEqual(el('rp-span-3').textContent, 'since 9:30 · 6h00m');
  assert.strictEqual(el('rp-updated').textContent, 'updated 14:24:30 ET · spot 7777.98');
  for (const i of COLS) assert.strictEqual(el(`rp-empty-${i}`).style.display, 'none');

  // Flow GEX on one chart only
  el('rp-mode-2-gex').listeners.click();
  assert.strictEqual(el('rp-mode-2-gex').className, 'active');
  assert.strictEqual(el('rp-mode-2-vol').className, '');
  assert(Math.abs(at(row[2], 0, 7780) - gexOf(120000)) < 1e-3, 'chart 2 shows call flow GEX');
  assert(Math.abs(at(row[2], 1, 7750) - 70000 * G_OTHER * 100 * SPOT) < 1e-3, 'chart 2 shows put flow GEX');
  assert.strictEqual(at(row[0], 0, 7780), 120000, 'chart 0 still shows volume');
  assert.strictEqual(at(row[3], 0, 7780), 120000, 'chart 3 still shows volume');
  const callGex = gexOf(120000) + 31 * 100 * G_OTHER * 100 * SPOT;     // 7700..7855, 32 strikes
  const putGex = (31 * 50 + 70000) * G_OTHER * 100 * SPOT;
  assert.strictEqual(el('rp-ctot-2').textContent, (callGex / 1e9).toFixed(1) + 'B');
  assert.strictEqual(el('rp-net-2').textContent, `Net +${((callGex - putGex) / 1e9).toFixed(1)}B`);
  assert.strictEqual(el('rp-net-2').className, 'rp-c');
  assert.strictEqual(el('rp-ctot-0').textContent, '123K');
  // the page's GEX unit per 1% move: Flow GEX x spot / 100; volume untouched
  global.gexUnitMult = s => s * 0.01;
  el('rp-mode-2-gex').listeners.click();
  assert(Math.abs(at(row[2], 0, 7780) - gexOf(120000) * SPOT * 0.01) < 1e-1, 'Flow GEX per 1% move');
  assert.strictEqual(at(row[0], 0, 7780), 120000, 'volume chart unchanged');
  delete global.gexUnitMult;
  el('rp-mode-2-gex').listeners.click();
  assert(Math.abs(at(row[2], 0, 7780) - gexOf(120000)) < 1e-3, 'back per point');
  const tip = row[2].options.plugins.tooltip.callbacks.label;
  assert.strictEqual(tip({ chart: row[2], dataset: { label: 'Calls' }, parsed: { x: 1.5e9 } }), 'Calls: 1.5B');
  assert.strictEqual(tip({ chart: row[0], dataset: { label: 'Calls' }, parsed: { x: 12345 } }), 'Calls: 12,345');
  assert.strictEqual(responses.length, 1, 'switching mode re-renders without a new request');
  assert.deepStrictEqual(JSON.parse(localStorage.getItem('rpCols')).map(c => c.m), ['vol', 'vol', 'gex', 'vol']);

  // time frame per chart: new request with that window, other charts unchanged
  el('rp-win-0').listeners.change({ target: { value: '60' } });
  await tick();
  assert.strictEqual(responses.at(-1), '/api/rolling_profile/SPX?windows=60,900,1800,session');
  assert.strictEqual(el('rp-span-0').textContent, 'actual 1m00s');
  assert.strictEqual(el('rp-span-1').textContent, 'actual 15m00s');
  // picking a window another chart already shows: drawn at once, asked for once
  const n = responses.length;
  el('rp-win-1').listeners.change({ target: { value: '1800' } });
  assert.strictEqual(el('rp-span-1').textContent, 'only 30m00s of history', 'drawn before the reply');
  await tick();
  assert.strictEqual(responses.length, n + 1);
  assert.strictEqual(responses.at(-1), '/api/rolling_profile/SPX?windows=60,1800,session');
  assert.strictEqual(at(row[1], 0, 7780), 120000, 'chart 1 keeps its own mode (volume)');
  assert.deepStrictEqual(JSON.parse(localStorage.getItem('rpCols')),
    [{ w: '60', m: 'vol' }, { w: '1800', m: 'vol' }, { w: '1800', m: 'gex' }, { w: 'session', m: 'vol' }]);
  // a reply from before a time-frame change lacks that window: the chart keeps what it shows
  only = ['session'];
  await poll(); await tick();
  only = null;
  assert.strictEqual(el('rp-span-0').textContent, 'actual 1m00s');
  assert.strictEqual(el('rp-empty-0').style.display, 'none');
  assert(row[0].data.datasets[0].data.length > 0);

  // zoom/pan configured on the strike axis, and synced across all four charts
  const z = row[0].options.plugins.zoom;
  assert.strictEqual(z.zoom.mode, 'y'); assert(z.zoom.wheel.enabled); assert(z.pan.enabled);
  const gesture = (c, lo, hi) => {             // what the zoom plugin does to the chart it acts on
    c.scales = { y: { min: lo, max: hi } }; c.options.scales.y.min = lo; c.options.scales.y.max = hi; };
  gesture(row[2], 7760, 7800);
  row[2].options.plugins.zoom.zoom.onZoom({ chart: row[2] });
  allView(7760, 7800, 'zooming one chart zooms them all');
  gesture(row[1], 7770, 7810);
  row[1].options.plugins.zoom.pan.onPan({ chart: row[1] });
  allView(7770, 7810, 'panning one chart pans them all');
  await poll(); await tick();
  allView(7770, 7810, 'the shared view survives a refresh');
  assert.strictEqual(el('rp-ctot-0').textContent, '121K', 'totals follow the zoomed view (7770..7810)');

  // double-click any chart: every chart returns to the slider range
  el('rp-canvas-1').listeners.dblclick();
  allView(7700, 7855, 'reset returns every chart to the slider range');
  row.forEach(c => assert.strictEqual(c.resets, 1, 'zoom plugin reset on each chart'));

  // range slider → new view on all charts, persisted
  el('rp-range').listeners.input({ target: { value: '150' } });
  allView(7625, 7930);
  assert.strictEqual(localStorage.getItem('rpRange'), '150');
  assert.strictEqual(el('rp-range-val').textContent, '±150');

  // full screen: a chart's header opens the dashboard modal with that chart, kept in sync
  let opened = '';
  global.openModal = h => { opened = h; };
  global.modalCJSChart = null;
  el('rp-head-2').listeners.dblclick();
  assert(opened.includes('FLOW GEX · 30 min'), 'modal titled with that chart\'s mode and window');
  assert.strictEqual(charts.length, 5, 'modal chart built');
  const modal = charts[4];
  assert.strictEqual(global.modalCJSChart, modal, 'closeModal() will destroy it');
  assert.deepStrictEqual(modal.data.datasets[0].data, row[2].data.datasets[0].data, 'modal shows chart 2');
  el('rp-mode-2-vol').listeners.click();
  assert.strictEqual(at(modal, 0, 7780), 120000, 'switching the chart switches its modal copy');
  gesture(modal, 7790, 7830);
  modal.options.plugins.zoom.zoom.onZoomComplete({ chart: modal });
  allView(7790, 7830, 'zooming the modal moves the row too');
  global.modalCJSChart = null;               // closeModal()
  el('rp-canvas-0').listeners.dblclick();
  allView(7625, 7930);
  assert.strictEqual(modal.resets || 0, 0, 'a closed modal is left alone');
  for (const c of ['rp-win-0', 'rp-mode-0-gex', 'rp-mode-0-vol', 'rp-range']) assert(el(c).listeners.dblclick, `${c} doesn't open the modal`);

  // no data yet → every chart says so
  empty = true;
  await poll(); await tick();
  for (const i of COLS) {
    assert.strictEqual(el(`rp-empty-${i}`).style.display, 'flex');
    assert.strictEqual(el(`rp-empty-${i}`).textContent, 'Waiting for 0DTE data…');
    assert.strictEqual(el(`rp-ctot-${i}`).textContent, '—');
  }
  assert.strictEqual(row[0].data.datasets[0].data.length, 0);
  assert.strictEqual(el('rp-updated').textContent, '—');
  empty = false;

  // tick labels: strikes without separators, amounts with K/M/B
  assert.strictEqual(row[0].options.scales.y.ticks.callback(7780), '7780');
  assert.strictEqual(row[0].options.scales.y.ticks.callback(7870.296350657283), '', 'a zoomed edge is not labelled');
  assert.strictEqual(row[0].options.scales.y.ticks.includeBounds, false);
  const fx = row[0].options.scales.x.ticks.callback;
  assert.deepStrictEqual([fx(950), fx(1500), fx(25000), fx(2.5e6), fx(3.2e9)], ['950', '1.5K', '25K', '2.5M', '3.2B']);
  // spot plugin draws only inside chart area
  await poll(); await tick();
  const drawn = []; const fakeCtx = new Proxy({}, { get: (_, k) => (...a) => drawn.push(k), set: () => true });
  row[0].plugins[0].afterDatasetsDraw({ scales: { y: { getPixelForValue: () => 100 } },
    chartArea: { left: 0, right: 200, top: 0, bottom: 400 }, ctx: fakeCtx });
  assert(drawn.includes('stroke'), 'spot line drawn');

  // a fresh load restores each chart's choices; bad saved values fall back to the defaults
  localStorage.setItem('rpCols', JSON.stringify([{ w: '3600', m: 'gex' }, { w: 'bogus', m: 'x' }]));
  page.inserted.length = 0; responses.length = 0;
  eval(SRC);
  await tick();
  const html2 = page.inserted[1].children[0]._html;
  assert(selectOf(html2, 0).includes('value="3600" selected') && html2.includes('id="rp-mode-0-gex" class="active"'));
  assert(selectOf(html2, 1).includes('value="900" selected') && html2.includes('id="rp-mode-1-vol" class="active"'));
  assert(selectOf(html2, 3).includes('value="session" selected'));
  assert.strictEqual(responses[0], '/api/rolling_profile/SPX?windows=3600,900,1800,session');

  // ?panel=rp1: one chart in its own window, its settings from the URL, saved under its own key
  global.PANEL = 'rp1';
  global.location = { search: '?panel=rp1&inst=2&w=900&m=gex' };
  page.inserted.length = 0; responses.length = 0;
  eval(SRC);
  await tick();
  const solo = page.inserted[1].children[0];
  assert(solo.className.includes('rp-solo'), 'one chart');
  assert(solo._html.includes('id="rp-win-0"') && !solo._html.includes('id="rp-win-1"'), 'just the one');
  assert(selectOf(solo._html, 0).includes('value="900" selected') && solo._html.includes('id="rp-mode-0-gex" class="active"'));
  assert.strictEqual(responses[0], '/api/rolling_profile/SPX?windows=900', 'asks for its window only');
  el('rp-mode-0-vol').listeners.click();
  assert.deepStrictEqual(JSON.parse(localStorage.getItem('rpCols-solo2')), [{ w: '900', m: 'vol' }], 'kept under its own key');
  assert.strictEqual(JSON.parse(localStorage.getItem('rpCols'))[0].w, '3600', "the panel's choices untouched");
  delete global.PANEL; delete global.location;
  console.log('JS HARNESS OK');
})().catch(e => { console.error('FAIL', e.message); process.exit(1); });
