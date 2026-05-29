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

import threading
import time
from flask import Flask
from gex import get_access_token
from price_history import sync_symbol
from streamer import SchwabStreamer
from log_setup import setup_logging
import delta_flow
import flow_alerts
import sse
import background
import routes

# ── App ───────────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder='../templates')

# ── Constants ─────────────────────────────────────────────────────────────────

SYMBOLS              = ['$SPX']         # GEX symbols  (SPY, QQQ temporarily removed)
PRICE_SYMBOLS        = ['$SPX', '/ES']  # price chart symbols
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

if __name__ == '__main__':
    setup_logging()
    print("Starting GEX Dashboard...")

    # Sync price history from Schwab REST API
    print("\nSyncing price history...")
    token = get_access_token()
    for symbol in PRICE_SYMBOLS:
        try:
            candles = sync_symbol(symbol, token)
            if symbol == '/ES':
                cutoff_ms = (time.time() - 5 * 86400) * 1000
                candles = [c for c in candles if c['datetime'] >= cutoff_ms]
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

    # Start background threads
    threading.Thread(target=background.gex_loop,   daemon=True).start()
    threading.Thread(target=background.price_loop, daemon=True).start()

    # Start WebSocket streamer
    _streamer = SchwabStreamer(
        on_candle=background.on_streamer_candle,
        on_flow_alert=background.on_flow_alert,
    )
    _streamer_ref[0] = _streamer
    _streamer.start()

    print("\nDashboard running at http://127.0.0.1:5000")
    try:
        app.run(debug=False, port=5000, threaded=True)
    except Exception as e:
        import traceback
        print(f"[FATAL] Flask crashed: {e}")
        traceback.print_exc()
        input("Press Enter to exit...")
