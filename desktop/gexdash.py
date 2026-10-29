"""
GEX Dash desktop: the dashboard's panels as their own Windows windows.

Each window is one panel of the dashboard page (?panel=price | gex | rolling | ddoi), served
by the dashboard server that is already running (Task Scheduler starts it at 6:14 AM PT):
closing these windows stops nothing, and page changes show on a refresh (F5) as in the
browser. Each window's place and size (and maximized) are remembered, per panel, in
%APPDATA%\\GEXDash\\windows.json; a place on a monitor that's gone falls back to the default.

  GEXDash.exe               all four windows
  GEXDash.exe price gex     just those
  GEXDash.exe --selftest out.json   open them off-screen (no focus taken, places not
                                    saved), check each page drew its panel, write the
                                    result, close: a check after a build, safe mid-session

Build the .exe: desktop\\build.bat  ->  desktop\\dist\\GEXDash.exe
From source:    desktop\\.venv\\Scripts\\python.exe desktop\\gexdash.py
"""
import json
import os
import sys
import threading
import time
import urllib.request

import webview

SERVER = os.environ.get('GEXDASH_URL', 'http://127.0.0.1:5000')
PANELS = {   # panel: (window title, default x, y, width, height)
    'price':   ('GEX Dash · Price',           40,  40, 1400, 820),
    'gex':     ('GEX Dash · GEX',             80,  80, 1400, 820),
    'rolling': ('GEX Dash · Rolling profile', 120, 120, 1400, 760),
    'ddoi':    ('GEX Dash · Dealer OI',       160, 160, 1100, 760),
}
HOME = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'GEXDash')
STATE = os.path.join(HOME, 'windows.json')
SAVE_EVERY_SEC = 1.0
BACKGROUND = '#0a0a0f'          # the page's own, so a window never flashes white

# shown while the server isn't up yet (before 6:14 AM, or mid-restart); loads the panel
# as soon as it answers
WAITING = """<html><body style="background:#0a0a0f;color:#9a9aac;font:14px 'Courier New',monospace;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div>Waiting for the dashboard server at {server} &hellip;<br><small>(Task Scheduler starts it at 6:14 AM PT)</small></div>
<script>
setInterval(() => fetch('{server}/api/history_days', {{mode: 'no-cors'}})
    .then(() => location.replace('{url}')).catch(() => {{}}), 3000);
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
        urllib.request.urlopen(SERVER + '/api/history_days', timeout=3).close()
        return True
    except Exception:
        return False


def open_windows(names: list, test: bool = False) -> dict:
    """test: off-screen, never taking the keyboard focus, places not saved (the self-test
    runs while trading)"""
    state, up = load_state(), server_up()
    windows = {}
    for i, name in enumerate(names):
        title, x, y, w, h = PANELS[name]
        saved = {} if test else state.get(name) or {}
        if {'x', 'y', 'w', 'h'} <= saved.keys() and on_a_screen(saved['x'], saved['y'], saved['w'], saved['h']):
            x, y, w, h = saved['x'], saved['y'], saved['w'], saved['h']
        if test:
            x, y = -20000 - 1600 * i, 0
        url = f'{SERVER}/?panel={name}'
        win = webview.create_window(
            title, url=url if up else None, html=None if up else WAITING.format(server=SERVER, url=url),
            x=x, y=y, width=w, height=h, min_size=(480, 320), maximized=bool(saved.get('max')),
            background_color=BACKGROUND, focus=not test)
        win.events.closed += lambda name=name: windows.pop(name, None)
        state[name] = dict(x=x, y=y, w=w, h=h, max=bool(saved.get('max')))
        windows[name] = win
    if test:
        return windows

    # Once a second, each open window as it is now: its place and size only while it's a
    # normal window (maximized / minimized it keeps the last normal one, plus 'max'). Read
    # from the window itself: pywebview runs each event handler on its own thread, so
    # 'maximized' and the resize it causes arrive in either order.
    def saver():
        while True:
            time.sleep(SAVE_EVERY_SEC)
            changed = False
            for name, win in list(windows.items()):
                try:
                    form = win.native
                    if form is None:
                        continue
                    mode = str(form.WindowState)          # Normal / Maximized / Minimized
                    now = dict(state[name])
                    if mode == 'Normal':
                        now.update(x=win.x, y=win.y, w=win.width, h=win.height, max=False)
                    elif mode == 'Maximized':
                        now['max'] = True
                except Exception:                          # closing: the form is gone
                    continue
                if now != state[name]:
                    state[name] = now
                    changed = True
            if changed:
                try:
                    save_state(state)
                except OSError:
                    pass

    threading.Thread(target=saver, daemon=True).start()
    return windows


def selftest(windows: dict, out: str) -> None:
    """each window: did the page draw its panel? Then close them all."""
    results = {}
    for win in list(windows.values()):
        win.events.loaded.wait(60)
    time.sleep(8)                         # the pages' own data loads
    for name, win in list(windows.items()):
        try:
            results[name] = win.evaluate_js(
                "(() => { const p = document.querySelector('.grid > .chart-div');"
                " return {panel: typeof PANEL === 'undefined' ? null : PANEL, title: document.title,"
                " size: p ? [p.offsetWidth, p.offsetHeight] : null,"
                " canvases: p ? [...p.querySelectorAll('canvas')].filter(c => c.offsetHeight > 50).length : 0}; })()")
        except Exception as e:
            results[name] = {'error': repr(e)}
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=1)
    for win in list(windows.values()):        # (closing one takes it out of `windows`)
        win.destroy()


def main(argv: list) -> None:
    test_out = None
    if '--selftest' in argv:
        i = argv.index('--selftest')
        test_out = argv[i + 1] if i + 1 < len(argv) else 'gexdash_selftest.json'
        argv = argv[:i] + argv[i + 2:]
    names = [a for a in argv if a in PANELS] or list(PANELS)
    windows = open_windows(names, test=bool(test_out))
    os.makedirs(HOME, exist_ok=True)
    # private_mode off + a storage folder: the page's own settings (GEX unit, zone view,
    # strikes slider...) are kept between runs, shared by the windows
    webview.start(selftest if test_out else None, (windows, test_out) if test_out else None,
                  private_mode=False, storage_path=os.path.join(HOME, 'webview'))


if __name__ == '__main__':
    main(sys.argv[1:])
