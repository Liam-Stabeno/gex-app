import os
import json
import threading
import time
from datetime import datetime
from queue import Queue, Empty
from flask import Flask, jsonify, render_template, Response, stream_with_context
from gex import get_access_token, fetch_option_chain, parse_gex, find_key_levels, get_watch_contracts
from price_history import sync_symbol, load_candles, fetch_candles, append_candles, backfill
from streamer import SchwabStreamer
from log_setup import setup_logging

app = Flask(__name__, template_folder='../templates')

SYMBOLS = ['$SPX', 'SPY', 'QQQ']               # GEX symbols (options chain)
PRICE_SYMBOLS = ['SPY', 'QQQ', '$SPX', '/ES']  # price chart symbols
REFRESH_INTERVAL = 60   # GEX refresh in seconds
PRICE_SYNC_INTERVAL = 60  # price sync in seconds

# In-memory cache
cache = {}
candle_cache = {}   # symbol -> list of candles
cache_lock = threading.Lock()

# Global streamer reference so GEX refresh can update the watch list
_streamer = None

# ── Server-Sent Events ──────────────────────────────────────────────────────────

sse_clients = []        # list of Queue objects, one per connected browser tab
sse_lock    = threading.Lock()


def sse_push(data: dict):
    """Push a JSON message to all connected SSE clients."""
    msg = f"data: {json.dumps(data)}\n\n"
    with sse_lock:
        for q in list(sse_clients):
            try:
                q.put_nowait(msg)
            except Exception:
                pass  # full queue — client is too slow, skip


# ── GEX ────────────────────────────────────────────────────────────────────────

def gex_to_dict(gex_df, spot):
    """Filter to significant strikes and serialize to lists."""
    if gex_df.empty:
        return {'strikes': [], 'net_gex': []}
    max_abs = gex_df['net_gex'].abs().max()
    if max_abs > 0:
        threshold = max_abs * 0.02   # keep strikes with >= 2% of peak gamma
        gex_df = gex_df[gex_df['net_gex'].abs() >= threshold]
    gex_df = gex_df.sort_values('strike')   # LW Charts requires ascending time order
    return {
        'strikes': gex_df['strike'].tolist(),
        'net_gex': gex_df['net_gex'].tolist(),
    }

def refresh_gex(symbol: str):
    try:
        token = get_access_token()
        chain = fetch_option_chain(symbol, token)
        gex_all, gex_0dte, gex_multi, spot, raw_df = parse_gex(chain)
        levels = find_key_levels(gex_all, spot)
        total_gex = float(gex_all['net_gex'].sum())

        levels_multi = find_key_levels(gex_multi, spot) if not gex_multi.empty else levels
        levels_0dte  = find_key_levels(gex_0dte,  spot) if not gex_0dte.empty  else {}

        def serialize_levels(lvl):
            return {k: (float(v) if v is not None else None) for k, v in lvl.items()}

        data = {
            'symbol': symbol.replace('$', '').replace('/', ''),
            'spot': spot,
            'total_gex': total_gex,
            'regime': 'POSITIVE' if total_gex > 0 else 'NEGATIVE',
            'levels_multi': serialize_levels(levels_multi),
            'levels_0dte':  serialize_levels(levels_0dte),
            'multi': gex_to_dict(gex_multi, spot),
            'zero':  gex_to_dict(gex_0dte,  spot),
            'has_0dte': not gex_0dte.empty,
            'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        # Extract option contracts to watch for flow monitoring
        call_wall  = levels_multi.get('call_wall')
        display_sym = symbol.replace('$', '').replace('/', '')  # SPX, SPY etc.
        watch      = get_watch_contracts(chain, call_wall, underlying=display_sym)

        with cache_lock:
            cache[symbol] = data

        # Update streamer watch list (streamer may not exist yet on first GEX run)
        if watch and _streamer:
            _streamer.update_options_watch(watch)

        print(f"[{datetime.now().strftime('%H:%M:%S')}] GEX updated: {symbol} @ {spot}"
              f"  |  watching {len(watch)} option contracts")

    except Exception as e:
        print(f"[ERROR] GEX {symbol}: {e}")


# ── Price history ───────────────────────────────────────────────────────────────

def refresh_price(symbol: str):
    try:
        token = get_access_token()
        fresh = fetch_candles(symbol, token, days=2, frequency=1)
        if not fresh:
            return
        added = append_candles(symbol, fresh)
        if added > 0:
            candles = load_candles(symbol)
            with cache_lock:
                candle_cache[symbol] = candles
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Price updated: {symbol} +{added} candles")
    except Exception as e:
        print(f"[ERROR] Price {symbol}: {e}")


# ── Background threads ──────────────────────────────────────────────────────────

def gex_loop():
    while True:
        for symbol in SYMBOLS:
            refresh_gex(symbol)
            time.sleep(2)
        time.sleep(REFRESH_INTERVAL)


def price_loop():
    while True:
        time.sleep(PRICE_SYNC_INTERVAL)
        for symbol in PRICE_SYMBOLS:
            refresh_price(symbol)
            time.sleep(2)


# ── API routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('dashboard.html')


@app.route('/api/gex/<symbol>')
def api_gex(symbol):
    key = f'${symbol}' if symbol == 'SPX' else f'/{symbol}' if symbol == 'ES' else symbol
    with cache_lock:
        data = cache.get(key) or cache.get(symbol)
    if not data:
        return jsonify({'error': 'No data yet'}), 202
    return jsonify(data)


@app.route('/api/price/<symbol>')
def api_price(symbol):
    from zoneinfo import ZoneInfo
    from datetime import time as dtime
    ET = ZoneInfo('America/New_York')
    key = f'${symbol}' if symbol == 'SPX' else f'/{symbol}' if symbol == 'ES' else symbol
    with cache_lock:
        candles = candle_cache.get(key, [])

    # Filter to regular market hours (9:30–16:00 ET) to exclude noisy extended-hours candles.
    # ES futures trade nearly 24 hrs so we keep all their candles.
    if symbol != 'ES':
        market_open  = dtime(9, 30)
        market_close = dtime(16, 0)
        def in_market_hours(c):
            t = datetime.fromtimestamp(c['datetime'] / 1000, tz=ET).time()
            return market_open <= t <= market_close
        candles = [c for c in candles if in_market_hours(c)]

    return jsonify([{
        'time':   int(c['datetime'] / 1000),   # Unix timestamp in seconds for LW Charts
        'open':   c['open'],
        'high':   c['high'],
        'low':    c['low'],
        'close':  c['close'],
        'volume': c['volume']
    } for c in candles])


@app.route('/api/all')
def api_all():
    with cache_lock:
        return jsonify(list(cache.values()))


@app.route('/api/stream')
def api_stream():
    """Server-Sent Events endpoint — pushes real-time candle updates to the browser."""
    q = Queue(maxsize=200)
    with sse_lock:
        sse_clients.append(q)

    def generate():
        try:
            # Handshake so the browser knows we're connected
            yield "data: {\"type\":\"connected\"}\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield msg
                except Empty:
                    yield ": keepalive\n\n"   # prevents proxy/browser timeout
        finally:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={
            'Cache-Control':    'no-cache',
            'X-Accel-Buffering': 'no',     # disable nginx buffering if behind a proxy
        }
    )


# ── Streamer candle callback ────────────────────────────────────────────────────

def on_streamer_candle(candle: dict):
    """
    Called by SchwabStreamer for each incoming 1-min candle update.
    Updates candle_cache and pushes to SSE clients.
    candle: { symbol, datetime (ms), open, high, low, close, volume }
    """
    raw_symbol = candle['symbol']
    # Map streamed symbol back to cache key (e.g. 'SPY' -> 'SPY', '/ES' -> '/ES')
    cache_key = raw_symbol

    # Update candle cache — append or replace last candle
    with cache_lock:
        existing = candle_cache.get(cache_key, [])
        if existing and existing[-1]['datetime'] == candle['datetime']:
            existing[-1] = candle   # update in-progress candle
        else:
            existing.append(candle)
        candle_cache[cache_key] = existing

    # Push to all SSE clients — time in seconds for JS (LW Charts needs seconds)
    sse_push({
        'type':    'candle',
        'symbol':  raw_symbol,
        'time':    candle['datetime'] // 1000,  # ms → seconds
        'open':    candle['open'],
        'high':    candle['high'],
        'low':     candle['low'],
        'close':   candle['close'],
        'volume':  candle['volume'],
    })

    ts = datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] Streamer candle: {raw_symbol} {candle["close"]:.2f}')


def on_flow_alert(alert: dict):
    """
    Called by SchwabStreamer when a weighted options volume spike is detected.
    alert: { symbol, underlying, strike, side, expiry_label, is_0dte,
              volume_delta, weighted_delta, last, bid, ask, direction }
    """
    sse_push({'type': 'flow_alert', **alert})
    ts = datetime.now().strftime('%H:%M:%S')
    direction = alert.get('direction', '?')
    print(f"[{ts}] FLOW  {alert['underlying']} {alert['strike']} "
          f"{alert['side'].upper()} {alert['expiry_label']}  "
          f"+{alert['volume_delta']:,} contracts  {direction}")


# ── Startup ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    setup_logging()
    print("Starting GEX Dashboard...")

    # Load price history and fill any gap since last run
    print("\nSyncing price history (gap-fill)...")
    token = get_access_token()
    for symbol in PRICE_SYMBOLS:
        # Use plain symbol for Schwab API — strip $ and /
        api_symbol = symbol if symbol not in ['$SPX', '$NDX', '/ES'] else symbol
        candles = sync_symbol(api_symbol, token)
        with cache_lock:
            candle_cache[symbol] = candles
        print(f"  {symbol}: {len(candles)} total candles")

    # Initial GEX fetch
    print("\nFetching initial GEX data...")
    for symbol in SYMBOLS:
        refresh_gex(symbol)

    # Start background threads
    threading.Thread(target=gex_loop, daemon=True).start()
    threading.Thread(target=price_loop, daemon=True).start()

    # Start WebSocket streamer (real-time candle updates + options flow via SSE)
    _streamer = SchwabStreamer(on_candle=on_streamer_candle, on_flow_alert=on_flow_alert)
    _streamer.start()

    print("\nDashboard running at http://127.0.0.1:5000")
    app.run(debug=False, port=5000, threaded=True)
