"""
dashboard.py — GEX Dashboard entry point.

Orchestrates startup: syncs price history, loads persisted state, fetches
initial GEX data, starts background threads and WebSocket streamer, then
serves the Flask app.

Modules:
    sse           — SSE client broadcasting
    flow_alerts   — daily flow alert persistence
    delta_flow    — cumulative dealer delta tracking
    background    — GEX/price refresh loops + streamer callbacks
    routes        — Flask API route handlers
"""

import logging
import os
import sys
import threading
import time
from flask import Flask
from gex import get_access_token, SchwabAuthError
from price_history import sync_symbol
from streamer import SchwabStreamer
from log_setup import setup_logging
import delta_flow
import flow_alerts
import sse
import background
import routes
import tos_rtd

# ── App ───────────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder='../templates')
# Re-read templates when they change: a page fix shows on a browser refresh instead
# of needing an app restart mid-session (the check is one file stat per page load).
app.config['TEMPLATES_AUTO_RELOAD'] = True
from rolling_profile import rolling_bp  # rolling profile (install_rolling_profile.py)
app.register_blueprint(rolling_bp)  # rolling profile (install_rolling_profile.py)

# ── Constants ─────────────────────────────────────────────────────────────────

SYMBOLS              = ['$SPX']                    # GEX symbols
PRICE_SYMBOLS        = ['$SPX', '/ES']   # price symbols synced from REST (VIX removed: Schwab
                                         # returned no candles and nothing displayed it)
REFRESH_INTERVAL     = 60               # GEX refresh cadence (seconds)
PRICE_SYNC_INTERVAL  = 60              # price sync cadence (seconds)

# ── Shared state ──────────────────────────────────────────────────────────────

cache                = {}
candle_cache         = {}
cache_lock           = threading.Lock()
csv_lock             = threading.Lock()
_gex_watch_by_symbol = {}
_gex_watch_lock      = threading.Lock()

# ── Route registration ────────────────────────────────────────────────────────

routes.init(cache, candle_cache, cache_lock)
routes.register(app)

# ── Startup ───────────────────────────────────────────────────────────────────

def _log_console_event(event: int) -> bool:
    """Record why Windows is ending the app (it exited 0xC000013A with nothing in the
    log on 2026-10-10). False lets the default handler go on and end the process."""
    names = {0: 'Ctrl+C', 1: 'Ctrl+Break', 2: 'console window closed',
             5: 'user logged off', 6: 'system shutdown'}
    print(f"[EXIT] Windows console event: {names.get(event, event)} — app stopping", flush=True)
    return False


def _hide_console() -> bool:
    """Hide this app's console window unless GEX_SHOW_CONSOLE=1 (start.bat sets it).
    Closing that window ends the app: on 2026-10-10 it stopped 3 times, each logged as
    'console window closed'. Output still goes to logs/app_<date>.log."""
    if os.environ.get('GEX_SHOW_CONSOLE') == '1':
        return False
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
            return True
    except Exception:
        pass
    return False


def _alert(message: str):
    """A Windows message box: with the console hidden, nobody would see a printed
    banner or answer an 'input()' prompt (it would wait forever, invisibly)."""
    print(message)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, 'GEX Dashboard', 0x10 | 0x10000 | 0x40000)
    except Exception:
        pass


if __name__ == '__main__':
    setup_logging()
    console_hidden = _hide_console()
    try:
        import win32api
        win32api.SetConsoleCtrlHandler(_log_console_event, True)
    except Exception as e:   # pywin32 missing: just no exit reason in the log
        print(f"[EXIT] console event logging unavailable: {e}")
    print("Starting GEX Dashboard..." + (" (console hidden; output in logs/)" if console_hidden else ""))

    # Sync price history from Schwab REST API
    print("\nSyncing price history...")
    try:
        token = get_access_token()
    except SchwabAuthError:
        # Banner already printed by gex.py — exit cleanly without a traceback.
        if console_hidden:
            _alert('GEX Dashboard could not start: the Schwab login has expired.\n\n'
                   'Run reauth.bat in C:\\Productivity\\TradingApp, then restart.bat.')
        sys.exit(1)
    for symbol in PRICE_SYMBOLS:
        try:
            # Memory holds only recent days (all of /ES was 780k candles, ~300 MB);
            # replay of older days reads the file (routes.api_price).
            candles = background.recent_candles(sync_symbol(symbol, token))
            with cache_lock:
                candle_cache[symbol] = candles
            print(f"  {symbol}: {len(candles)} candles loaded")
        except Exception as e:
            print(f"  [ERROR] {symbol} sync failed: {e}")

    # Restore persisted state
    delta_flow.set_sse_push(sse.push)
    delta_flow.load_today()
    flow_alerts.load_today()

    # Wire background module with shared state
    _streamer_ref = [None]
    background.init(
        cache=cache, candle_cache=candle_cache, cache_lock=cache_lock,
        csv_lock=csv_lock, streamer_ref=_streamer_ref,
        gex_watch=_gex_watch_by_symbol, gex_watch_lock=_gex_watch_lock,
        symbols=SYMBOLS, price_symbols=PRICE_SYMBOLS,
        refresh_interval=REFRESH_INTERVAL, price_sync_interval=PRICE_SYNC_INTERVAL,
    )

    # Initial GEX fetch
    print("\nFetching initial GEX data...")
    for symbol in SYMBOLS:
        background.refresh_gex(symbol)

    # Start TOS RTD if available (lower latency options quotes)
    tos_rtd.start()

    # Start background threads
    threading.Thread(target=background.gex_loop,      daemon=True).start()
    threading.Thread(target=background.price_loop,    daemon=True).start()
    threading.Thread(target=background.live_gex_loop, daemon=True).start()
    threading.Thread(target=background.daily_jobs_loop, daemon=True).start()

    # Start WebSocket streamer
    _streamer = SchwabStreamer(
        on_candle=background.on_streamer_candle,
        on_flow_alert=background.on_flow_alert,
        on_options_quote=background.on_options_quote,
        on_volume_split=background.on_volume_split,
    )
    _streamer_ref[0] = _streamer
    _streamer.start()

    # Open Chrome once Flask is ready
    import threading as _t
    def _open_browser():
        time.sleep(1.5)
        import webbrowser
        try:
            webbrowser.get('chrome').open('http://127.0.0.1:5000')
        except webbrowser.Error:
            webbrowser.open('http://127.0.0.1:5000')
    _t.Thread(target=_open_browser, daemon=True).start()

    print("\nDashboard running at http://127.0.0.1:5000")
    # Request lines (~14k a day, mostly the browser polling) only when they fail
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    try:
        app.run(debug=False, port=5000, threaded=True)
    except Exception as e:
        import traceback
        print(f"[FATAL] Flask crashed: {e}")
        traceback.print_exc()
        if console_hidden:
            _alert(f'GEX Dashboard stopped: {e}\n\nDetails are in logs\\app_<date>.log. '
                   'restart.bat starts it again.')
        else:
            input("Press Enter to exit...")
