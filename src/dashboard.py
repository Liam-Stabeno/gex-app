import os
import json
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, render_template
from gex import get_access_token, fetch_option_chain, parse_gex, find_key_levels
from price_history import sync_symbol, load_candles, fetch_candles, append_candles, backfill

app = Flask(__name__, template_folder='../templates')

SYMBOLS = ['$SPX', 'SPY', 'QQQ', '$NDX']        # GEX symbols (options chain)
PRICE_SYMBOLS = ['SPY', 'QQQ', '$SPX', '$NDX', '/ES']  # price chart symbols
REFRESH_INTERVAL = 60   # GEX refresh in seconds
PRICE_SYNC_INTERVAL = 60  # price sync in seconds

# In-memory cache
cache = {}
candle_cache = {}   # symbol -> list of candles
cache_lock = threading.Lock()


# ── GEX ────────────────────────────────────────────────────────────────────────

def refresh_gex(symbol: str):
    try:
        token = get_access_token()
        chain = fetch_option_chain(symbol, token)
        gex_by_strike, spot, raw_df = parse_gex(chain)
        levels = find_key_levels(gex_by_strike, spot)
        total_gex = float(gex_by_strike['net_gex'].sum())

        data = {
            'symbol': symbol.replace('$', '').replace('/', ''),
            'spot': spot,
            'total_gex': total_gex,
            'regime': 'POSITIVE' if total_gex > 0 else 'NEGATIVE',
            'levels': {k: (float(v) if v is not None else None) for k, v in levels.items()},
            'strikes': gex_by_strike['strike'].tolist(),
            'net_gex': gex_by_strike['net_gex'].tolist(),
            'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        with cache_lock:
            cache[symbol] = data

        print(f"[{datetime.now().strftime('%H:%M:%S')}] GEX updated: {symbol} @ {spot}")

    except Exception as e:
        print(f"[ERROR] GEX {symbol}: {e}")


# ── Price history ───────────────────────────────────────────────────────────────

def refresh_price(symbol: str):
    try:
        token = get_access_token()
        fresh = fetch_candles(symbol, token, days=1, frequency=1)
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
    key = f'${symbol}' if symbol in ['SPX', 'NDX'] else f'/{symbol}' if symbol == 'ES' else symbol
    with cache_lock:
        data = cache.get(key) or cache.get(symbol)
    if not data:
        return jsonify({'error': 'No data yet'}), 202
    return jsonify(data)


@app.route('/api/price/<symbol>')
def api_price(symbol):
    from zoneinfo import ZoneInfo
    ET = ZoneInfo('America/New_York')
    with cache_lock:
        candles = candle_cache.get(symbol, [])
    recent = candles[-390:]
    return jsonify([{
        'time': datetime.fromtimestamp(c['datetime'] / 1000, tz=ET).strftime('%H:%M'),
        'open':  c['open'],
        'high':  c['high'],
        'low':   c['low'],
        'close': c['close'],
        'volume': c['volume']
    } for c in recent])


@app.route('/api/all')
def api_all():
    with cache_lock:
        return jsonify(list(cache.values()))


# ── Startup ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
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

    print("\nDashboard running at http://127.0.0.1:5000")
    app.run(debug=False, port=5000)
