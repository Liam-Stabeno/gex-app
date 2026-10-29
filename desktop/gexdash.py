"""
GEX Dash desktop: a control panel that pops the dashboard's panels out into their own
Windows windows.

GEXDash.exe opens the control panel only. Its buttons open a panel of the dashboard page
(?panel=price | gex | rolling | ddoi, or the whole page) in its own window, or bring that
window to the front when it's already open; ✕ closes one; closing the control panel closes
them all. A second GEXDash.exe brings the open control panel forward instead of starting
another. Each window's place and size (and maximized) are remembered, per panel, in
%APPDATA%\\GEXDash\\windows.json; a place on a monitor that's gone falls back to the default.

The windows show the dashboard server that is already running (Task Scheduler starts it at
6:14 AM PT): closing them stops nothing, and page changes show on a refresh (F5).

  GEXDash.exe               the control panel
  GEXDash.exe price gex     the control panel, and those opened
  GEXDash.exe --selftest out.json   all of it off-screen (no focus taken, places not saved):
                                    click every button, check each window drew its panel,
                                    write the result, close. A check after a build, safe
                                    mid-session

Build the .exe: desktop\\build.bat  ->  desktop\\dist\\GEXDash.exe
From source:    desktop\\.venv\\Scripts\\python.exe desktop\\gexdash.py
"""
import ctypes
import json
import os
import sys
import threading
import time
import urllib.request

import webview

SERVER = os.environ.get('GEXDASH_URL', 'http://127.0.0.1:5000')
CONTROL = 'control'
CONTROL_TITLE = 'GEX Dash'      # found by a second GEXDash.exe, to bring it forward
PANELS = {   # panel: (window title, default x, y, width, height); 'all' = the whole page
    'price':   ('GEX Dash · Price',           40,  40, 1400, 820),
    'gex':     ('GEX Dash · GEX',             80,  80, 1400, 820),
    'rolling': ('GEX Dash · Rolling profile', 120, 120, 1400, 760),
    'ddoi':    ('GEX Dash · Dealer OI',       160, 160, 1100, 760),
    'all':     ('GEX Dash · Dashboard',       200, 60, 1500, 900),
}
LABELS = {'price': 'Price', 'gex': 'GEX', 'rolling': 'Rolling profile', 'ddoi': 'Dealer OI', 'all': 'Whole dashboard'}
CONTROL_PLACE = (1500, 40, 300, 430)
HOME = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'GEXDash')
STATE = os.path.join(HOME, 'windows.json')
SAVE_EVERY_SEC = 1.0
BACKGROUND = '#0a0a0f'          # the page's own, so a window never flashes white
MUTEX = 'Local\\GEXDashControlPanel'
user32 = ctypes.windll.user32
_mutex = None                   # held while the control panel is open (already_running)

# shown while the server isn't up yet (before 6:14 AM, or mid-restart); loads the panel
# as soon as it answers
WAITING = """<html><body style="background:#0a0a0f;color:#9a9aac;font:14px 'Courier New',monospace;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div>Waiting for the dashboard server at {server} &hellip;<br><small>(Task Scheduler starts it at 6:14 AM PT)</small></div>
<script>
setInterval(() => fetch('{server}/api/rtd/status', {{mode: 'no-cors'}})
    .then(() => location.replace('{url}')).catch(() => {{}}), 3000);
</script></body></html>"""

PANEL_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  body { background: #0a0a0f; color: #e0e0e0; font: 12px 'Courier New', monospace; margin: 0; padding: 14px;
         user-select: none; }
  h1 { font-size: 15px; letter-spacing: 4px; color: #00d4ff; margin: 0 0 2px; }
  .srv { font-size: 10px; color: #8a8a9c; margin-bottom: 12px; }
  .srv b { color: #ff4466; }  .srv b.up { color: #00ff88; }
  .row { display: flex; gap: 6px; margin-bottom: 7px; }
  .open { flex: 1; text-align: left; background: #12121a; color: #d8d8e4; border: 1px solid #2a2a3e; border-radius: 5px;
          font: 13px 'Courier New', monospace; padding: 9px 10px; cursor: pointer; display: flex; align-items: center; gap: 8px; }
  .open:hover { border-color: #00d4ff; color: #fff; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #2a2a3a; flex-shrink: 0; }
  .on .dot { background: #00ff88; box-shadow: 0 0 6px #00ff8899; }
  .on .open { border-color: #1f5c40; }
  .x { width: 30px; background: #12121a; color: #6f6f82; border: 1px solid #2a2a3e; border-radius: 5px; cursor: pointer;
       font: 13px 'Courier New', monospace; visibility: hidden; }
  .on .x { visibility: visible; }  .x:hover { color: #ff4466; border-color: #ff4466; }
  .all { display: flex; gap: 6px; margin-top: 12px; }
  .all button { flex: 1; background: #0d1b24; color: #00d4ff; border: 1px solid #00d4ff66; border-radius: 5px;
                font: 12px 'Courier New', monospace; padding: 7px; cursor: pointer; }
  .all button:hover { border-color: #00d4ff; }
  .hint { color: #6f6f82; font-size: 10px; line-height: 1.5; margin-top: 12px; }
</style></head><body>
  <h1>GEX DASH</h1>
  <div class="srv">server <b id="srv">checking&hellip;</b></div>
  <div id="rows">%ROWS%</div>
  <div class="all"><button id="open-all">Open all</button><button id="close-all">Close all</button></div>
  <div class="hint">A button opens that chart in its own window, or brings it to the front.
    &#10005; closes one. Closing this panel closes them all.</div>
<script>
  const api = () => window.pywebview && window.pywebview.api;
  // a click before the Python side is connected waits for it instead of doing nothing
  const ready = new Promise(r => api() ? r() : window.addEventListener('pywebviewready', r));
  const call = (fn, ...a) => ready.then(() => api()[fn](...a)).then(refresh);
  document.querySelectorAll('[data-open]').forEach(b => b.onclick = () => call('open', b.dataset.open));
  document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => call('close', b.dataset.close));
  document.getElementById('open-all').onclick = () => call('open_all');
  document.getElementById('close-all').onclick = () => call('close_all');
  async function refresh() {
    const x = api();
    if (!x) return;
    const s = await x.status();
    document.querySelectorAll('.row').forEach(r => r.classList.toggle('on', s.open.includes(r.dataset.panel)));
    const srv = document.getElementById('srv');
    srv.textContent = s.server ? 'running' : 'not answering (starts 6:14 AM PT)';
    srv.className = s.server ? 'up' : '';
  }
  window.addEventListener('pywebviewready', refresh);
  setInterval(refresh, 2000);
</script></body></html>"""


def load_state() -> dict:
    try:
        with open(STATE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(HOME, exist_ok=True)
    tmp = STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE)


def on_a_screen(x, y, w, h) -> bool:
    """at least a 100 x 60 corner of the window's title bar area is on some monitor"""
    try:
        screens = webview.screens() if callable(webview.screens) else webview.screens
    except Exception:
        return True
    for s in screens:
        if x + w - 100 > s.x and x + 100 < s.x + s.width and y + 60 > s.y and y < s.y + s.height - 60:
            return True
    return False


def server_up() -> bool:
    try:
        urllib.request.urlopen(SERVER + '/api/rtd/status', timeout=2).close()
        return True
    except Exception:
        return False


def hwnd(win):
    return ctypes.c_void_p(win.native.Handle.ToInt64())


def to_front(win) -> None:
    try:
        h = hwnd(win)
        user32.ShowWindow(h, 9 if user32.IsIconic(h) else 5)     # SW_RESTORE / SW_SHOW
        user32.SetForegroundWindow(h)
    except Exception:
        pass


class App:
    """test: off-screen, never taking the keyboard focus, places not saved (the self-test
    runs while trading)"""

    def __init__(self, test: bool = False):
        self.test = test
        self.state = {} if test else load_state()
        self.windows = {}                 # name -> pywebview window, the control panel too
        self.lock = threading.Lock()
        self.opened = 0                   # for the self-test's off-screen places
        self.events_log = {}              # self-test: name -> its window's events

    def place(self, name, x, y, w, h):
        saved = self.state.get(name) or {}
        if {'x', 'y', 'w', 'h'} <= saved.keys() and on_a_screen(saved['x'], saved['y'], saved['w'], saved['h']):
            x, y, w, h = saved['x'], saved['y'], saved['w'], saved['h']
        if self.test:
            x, y = -20000 - 1600 * self.opened, 0
            self.opened += 1
        self.state[name] = dict(x=x, y=y, w=w, h=h, max=bool(saved.get('max')))
        return x, y, w, h, bool(saved.get('max'))

    def track(self, name, win):
        self.windows[name] = win
        win.events.closed += lambda: self.windows.pop(name, None)
        if self.test:                     # what each window did, for the self-test's report
            t0 = time.time()
            log = self.events_log.setdefault(name, [])
            for ev in ('shown', 'loaded', 'closed'):
                getattr(win.events, ev).__iadd__(lambda ev=ev: log.append(f'{ev} {time.time() - t0:.1f}s'))

    def control(self):
        rows = ''.join(
            f'<div class="row" data-panel="{p}"><button class="open" data-open="{p}"><span class="dot"></span>{LABELS[p]}</button>'
            f'<button class="x" data-close="{p}" title="Close it">&#10005;</button></div>' for p in PANELS)
        x, y, w, h, mx = self.place(CONTROL, *CONTROL_PLACE)
        win = webview.create_window(CONTROL_TITLE, html=PANEL_HTML.replace('%ROWS%', rows), js_api=Api(self),
                                    x=x, y=y, width=w, height=h, min_size=(260, 300), maximized=mx,
                                    background_color=BACKGROUND, focus=not self.test)
        self.track(CONTROL, win)
        win.events.closed += self.close_all       # no control panel: nothing to open more with
        return win

    def open(self, name):
        """open that panel's window, or bring it to the front when it's open"""
        if name not in PANELS:
            return
        with self.lock:
            win = self.windows.get(name)
            if win is None:
                title, *place = PANELS[name]
                x, y, w, h, mx = self.place(name, *place)
                url = SERVER + ('/' if name == 'all' else f'/?panel={name}')
                up = server_up()
                win = webview.create_window(
                    title, url=url if up else None, html=None if up else WAITING.format(server=SERVER, url=url),
                    x=x, y=y, width=w, height=h, min_size=(480, 320), maximized=mx,
                    background_color=BACKGROUND, focus=not self.test)
                self.track(name, win)
                return
        to_front(win)

    def close(self, name):
        win = self.windows.get(name)
        if win is not None and name != CONTROL:
            win.destroy()

    def close_all(self):
        for name in [n for n in self.windows if n != CONTROL]:
            self.close(name)

    # Once a second, each open window as it is now: its place and size only while it's a
    # normal window (maximized / minimized it keeps the last normal one, plus 'max'). Read
    # from the window itself: pywebview runs each event handler on its own thread, so
    # 'maximized' and the resize it causes arrive in either order.
    def saver(self):
        while True:
            time.sleep(SAVE_EVERY_SEC)
            changed = False
            for name, win in list(self.windows.items()):
                try:
                    form = win.native
                    if form is None:
                        continue
                    mode = str(form.WindowState)          # Normal / Maximized / Minimized
                    now = dict(self.state.get(name, {}))
                    if mode == 'Normal':
                        now.update(x=win.x, y=win.y, w=win.width, h=win.height, max=False)
                    elif mode == 'Maximized':
                        now['max'] = True
                except Exception:                          # closing: the form is gone
                    continue
                if now != self.state.get(name):
                    self.state[name] = now
                    changed = True
            if changed and not self.test:
                try:
                    save_state(self.state)
                except OSError:
                    pass


class Api:
    """what the control panel's buttons call (window.pywebview.api.*)"""

    def __init__(self, app):
        self._app = app

    def open(self, name):
        self._app.open(name)

    def close(self, name):
        self._app.close(name)

    def open_all(self):
        for name in PANELS:
            if name != 'all':
                self._app.open(name)

    def close_all(self):
        self._app.close_all()

    def status(self):
        return {'open': [n for n in self._app.windows if n != CONTROL], 'server': server_up()}


def already_running() -> bool:
    """another GEXDash.exe holds the mutex: bring its control panel forward instead"""
    global _mutex
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX)
    if ctypes.windll.kernel32.GetLastError() != 183:           # ERROR_ALREADY_EXISTS
        return False
    h = user32.FindWindowW(None, CONTROL_TITLE)
    if h:
        user32.ShowWindow(h, 9 if user32.IsIconic(h) else 5)
        user32.SetForegroundWindow(h)
    return True


def selftest(app: App, out: str) -> None:
    """click every button, check each window drew its panel, a second click opens no copy,
    ✕ closes one; then close it all"""
    control = app.windows[CONTROL]
    control.events.loaded.wait(60)
    for _ in range(100):                            # the buttons reach Python once its api is in
        if control.evaluate_js("!!(window.pywebview && window.pywebview.api && window.pywebview.api.open)"):
            break
        time.sleep(0.2)
    click = lambda sel: control.evaluate_js(f"document.querySelector('{sel}').click()")
    for name in PANELS:
        click(f'[data-open="{name}"]')
        time.sleep(0.5)
    for name in PANELS:
        if name in app.windows:
            app.windows[name].events.loaded.wait(60)
    time.sleep(8)                                   # the pages' own data loads
    results = {'opened': sorted(n for n in app.windows if n != CONTROL)}
    for name in PANELS:
        win = app.windows.get(name)
        if win is None:
            results[name] = 'not opened'
            continue
        try:
            results[name] = win.evaluate_js(
                "(() => { const p = document.querySelector('.grid > .chart-div');"
                " return {panel: typeof PANEL === 'undefined' ? null : PANEL, title: document.title,"
                " size: p ? [p.offsetWidth, p.offsetHeight] : null,"
                " canvases: [...document.querySelectorAll('canvas')].filter(c => c.offsetHeight > 50).length}; })()")
        except Exception as e:
            results[name] = {'error': repr(e)}
    click('[data-open="gex"]')                      # open already: no second window
    time.sleep(1)
    results['after_second_click'] = sorted(n for n in app.windows if n != CONTROL)
    click('[data-close="ddoi"]')
    time.sleep(2)
    results['after_close_ddoi'] = sorted(n for n in app.windows if n != CONTROL)
    results['dots_on'] = control.evaluate_js("[...document.querySelectorAll('.row.on')].map(r => r.dataset.panel)")
    results['events'] = app.events_log
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=1)
    control.destroy()                               # closes the rest


def main(argv: list) -> None:
    test_out = None
    if '--selftest' in argv:
        i = argv.index('--selftest')
        test_out = argv[i + 1] if i + 1 < len(argv) else 'gexdash_selftest.json'
        argv = argv[:i] + argv[i + 2:]
    if not test_out and already_running():
        return
    app = App(test=bool(test_out))
    app.control()
    threading.Thread(target=app.saver, daemon=True).start()
    first = [a for a in argv if a in PANELS]
    os.makedirs(HOME, exist_ok=True)

    def started():
        for name in first:
            app.open(name)
        if test_out:
            selftest(app, test_out)

    # private_mode off + a storage folder: the page's own settings (GEX unit, zone view,
    # strikes slider...) are kept between runs, shared by the windows
    # (the self-test has its own: it runs beside an open GEXDash.exe)
    webview.start(started, private_mode=False,
                  storage_path=os.path.join(HOME, 'webview-selftest' if test_out else 'webview'))


if __name__ == '__main__':
    main(sys.argv[1:])
