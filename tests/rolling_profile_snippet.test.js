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
let lastChart = null, created = 0;
global.Chart = function (ctx, cfg) { created++; lastChart = this; this.data = cfg.data; this.options = cfg.options;
  this.plugins = cfg.plugins; this.update = () => {}; this.resetZoom = () => { this.resets = (this.resets || 0) + 1; }; };
const responses = [];
global.fetch = async url => { responses.push(url); const w = Number(url.split('window=')[1]);
  const strikes = [], call = [], put = [];
  for (let k = 7400; k <= 8150; k += 5) { strikes.push(k); call.push(k === 7780 ? 120000 : 100); put.push(k === 7750 ? 70000 : 50); }
  return { ok: true, json: async () => ({ symbol: 'SPX', window_sec: w, covered_sec: w, partial: w === 3600,
    spot: 7777.98, updated: '14:24:30', strikes, call, put }) }; };
global.setInterval = () => 0;
eval(SRC);
(async () => {
  await new Promise(r => setTimeout(r, 20));
  const assert = require('assert');

  // own section: label + full-width grid inserted after #gex-grid
  assert.strictEqual(page.inserted.length, 2, 'section label + grid inserted');
  assert.strictEqual(page.inserted[0].textContent, 'ROLLING PROFILE');
  assert.strictEqual(page.inserted[1].className, 'grid rp-grid');
  const panel = page.inserted[1].children[0];
  assert(panel.className.includes('gex-chart-div'), 'resizable like the GEX panels');
  const html = panel._html;
  for (const l of ['1 min','2 min','3 min','5 min','10 min','15 min','30 min','1 hr']) assert(html.includes(`>${l}<`), 'option ' + l);
  assert(html.includes('value="1800" selected'), 'default 30 min');
  assert(html.includes('id="rp-range"') && html.includes('value="75"'), 'range slider defaults to ±75');
  assert.strictEqual(responses[0], '/api/rolling_profile/SPX?window=1800');
  assert.strictEqual(created, 1);

  // view: ±75 around spot; data reaches further so zooming out shows more strikes
  assert.strictEqual(lastChart.options.scales.y.min, 7700);
  assert.strictEqual(lastChart.options.scales.y.max, 7855);
  const ys = lastChart.data.datasets[0].data.map(p => p.y);
  assert(Math.min(...ys) < 7700 && Math.max(...ys) > 7855, 'data wider than the view');
  assert(Math.min(...ys) >= 7777.98 - 350 && Math.max(...ys) <= 7777.98 + 350, 'data capped at ±350');
  assert.strictEqual(lastChart.options.indexAxis, 'y');
  assert.strictEqual(el('rp-call-tot').textContent, '123K', 'totals cover the visible range only');
  assert.strictEqual(el('rp-span').textContent, 'actual 30m00s · 14:24:30 ET');
  assert.strictEqual(el('rp-empty').style.display, 'none');

  // zoom/pan configured on the strike axis
  const z = lastChart.options.plugins.zoom;
  assert.strictEqual(z.zoom.mode, 'y'); assert(z.zoom.wheel.enabled); assert(z.pan.enabled);

  // range slider → new view, persisted
  el('rp-range').listeners.input({ target: { value: '150' } });
  assert.strictEqual(lastChart.options.scales.y.min, 7625);
  assert.strictEqual(lastChart.options.scales.y.max, 7930);
  assert.strictEqual(localStorage.getItem('rpRange'), '150');
  assert.strictEqual(el('rp-range-val').textContent, '±150');

  // after a user zoom, refreshes keep the user's view; double-click resets it
  const resetsBefore = lastChart.resets || 0;
  z.zoom.onZoomComplete({ chart: lastChart });
  lastChart.options.scales.y.min = 7760; lastChart.options.scales.y.max = 7800;
  el('rp-window').listeners.change({ target: { value: '3600' } });
  await new Promise(r => setTimeout(r, 20));
  assert.strictEqual(lastChart.options.scales.y.min, 7760, 'zoom survives refresh');
  el('rp-canvas').listeners.dblclick();
  assert.strictEqual(lastChart.resets, resetsBefore + 1);
  assert.strictEqual(lastChart.options.scales.y.min, 7625, 'reset returns to the slider range');

  // switching window → partial warning, chart reused, choice persisted
  assert.strictEqual(responses.at(-1), '/api/rolling_profile/SPX?window=3600');
  assert.strictEqual(created, 1, 'chart not rebuilt');
  assert.strictEqual(el('rp-span').className, 'rp-warn');
  assert.strictEqual(el('rp-span').textContent, 'only 1h00m of history');
  assert.strictEqual(localStorage.getItem('rpWindow'), '3600');

  // full screen: header double-click opens the dashboard modal with its own chart
  let opened = 0;
  global.openModal = () => { opened++; };
  global.modalCJSChart = null;
  el('rp-header').listeners.dblclick();
  assert.strictEqual(opened, 1);
  assert.strictEqual(created, 2, 'modal chart built');
  assert.strictEqual(global.modalCJSChart, lastChart, 'closeModal() will destroy it');

  // y tick labels: no thousands separator
  assert.strictEqual(lastChart.options.scales.y.ticks.callback(7780), '7780');
  // spot plugin draws only inside chart area
  const calls = []; const fakeCtx = new Proxy({}, { get: (_, k) => (...a) => calls.push(k), set: () => true });
  lastChart.plugins[0].afterDatasetsDraw({ scales: { y: { getPixelForValue: () => 100 } },
    chartArea: { left: 0, right: 200, top: 0, bottom: 400 }, ctx: fakeCtx });
  assert(calls.includes('stroke'), 'spot line drawn');
  console.log('JS HARNESS OK');
})().catch(e => { console.error('FAIL', e.message); process.exit(1); });
