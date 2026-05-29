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
import delta_flow

app = Flask(__name__, template_folder='../templates')

SYMBOLS = ['$SPX']  # GEX symbols (options chain)  # SPY, QQQ temporarily removed
PRICE_SYMBOLS = ['$SPX', '/ES']  # price chart symbols
REFRESH_INTERVAL = 60   # GEX refresh in seconds
PRICE_SYNC_INTERVAL = 60  # price sync in seconds

# In-memory cache
cache = {}
candle_cache = {}   # symbol -> list of candles
cache_lock = threading.Lock()

# Separate lock for CSV file writes — prevents the streamer and price_loop
# from calling append_candles concurrently and corrupting the CSV.
csv_lock = threading.Lock()

# Global streamer reference so GEX refresh can update the watch list
_streamer = None

# Per-symbol options watch contracts. Only SPX is passed to the streamer —
# SPX 0DTE hedging flows directly into ES futures and is the only flow that
# materially moves price intraday. SPY/QQQ GEX refreshes but isn't streamed.
_gex_watch_by_symbol: dict = {}   # 'SPX' -> [...], 'SPY' -> [...], etc.
_gex_watch_lock = threading.Lock()

# ── Flow alert persistence ──────────────────────────────────────────────────────

_SRC_DIR     = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SRC_DIR)
_DATA_DIR    = os.path.join(_PROJECT_DIR, 'data')

_flow_alerts      = []          # list of alert dicts for today
_flow_alerts_lock = threading.Lock()


def _flow_alerts_path(date_obj) -> str:
    return os.path.join(_DATA_DIR, f'flow_alerts_{date_obj}.json')


def _save_flow_alerts():
    """Append-save all today's alerts. Called inside _flow_alerts_lock."""
    today = datetime.now().strftime('%Y-%m-%d')
    path  = _flow_alerts_path(today)
    try:
        with open(path, 'w') as f:
            json.dump(_flow_alerts, f)
    except Exception as e:
        print(f'[ERROR] flow_alerts save: {e}')


def load_flow_alerts_today():
    """Load today's persisted flow alerts on startup."""
    today = datetime.now().strftime('%Y-%m-%d')
    path  = _flow_alerts_path(today)
    if not os.path.exists(path):
        print('[flow_alerts] No saved alerts for today — starting fresh')
        return
    try:
        with open(path) as f:
            saved = json.load(f)
        with _flow_alerts_lock:
            _flow_alerts.extend(saved)
        print(f'[flow_alerts] Restored {len(saved)} alerts from today')
    except Exception as e:
        print(f'[ERROR] flow_alerts load: {e}')


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
        # SPX has daily expirations at 5-pt spacing — 200 strikes × 40+ expiries
        # exceeds Schwab's body limit.  SPY/QQQ are safe at 200.
        strike_count = 150 if symbol in ('$SPX', 'SPX') else 200
        chain = fetch_option_chain(symbol, token, strike_count=strike_count)
        gex_all, gex_0dte, gex_multi, spot, raw_df = parse_gex(chain)
        levels = find_key_levels(gex_all, spot)
        total_gex = float(gex_all['net_gex'].sum())

        levels_multi = find_key_levels(gex_multi, spot) if not gex_multi.empty else levels
        levels_0dte  = find_key_levels(gex_0dte,  spot) if not gex_0dte.empty  else {}

        def serialize_levels(lvl):
            return {k: (float(v) if v is not None else None) for k, v in lvl.items()}

        # Extract the actual 0DTE expiry date from the chain
        # (key format is "2026-05-22:0" — split off the ":0" suffix)
        odte_date = None
        for exp_key in chain.get('callExpDateMap', {}).keys():
            if exp_key.endswith(':0'):
                odte_date = exp_key.split(':')[0]   # "2026-05-22"
                break

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
            'odte_date': odte_date,   # "YYYY-MM-DD" of the 0DTE expiry (today or next open day)
            'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        # Extract option contracts to watch for flow monitoring.
        # Only SPX contracts are passed to the streamer — SPX 0DTE hedging goes
        # directly into ES futures and is the only flow that materially moves price.
        # SPY and QQQ GEX charts still refresh normally; they just aren't streamed.
        call_wall   = levels_multi.get('call_wall')
        display_sym = symbol.replace('$', '').replace('/', '')   # 'SPX', 'SPY', 'QQQ'
        watch       = get_watch_contracts(chain, call_wall, underlying=display_sym)

        with cache_lock:
            cache[symbol] = data

        if display_sym == 'SPX':
            # Full-book delta flow from chain volume diff (every strike, every expiry)
            new_snap = delta_flow.extract_chain_snapshot(chain)
            delta_flow.process_chain_snapshot(new_snap)

            # Streaming watch list — high-OI strikes for real-time flow alerts
            with _gex_watch_lock:
                _gex_watch_by_symbol['SPX'] = watch
                all_contracts = list(_gex_watch_by_symbol.get('SPX', []))
            if all_contracts and _streamer:
                n_0dte  = sum(1 for c in all_contracts if c.get('is_0dte'))
                n_multi = len(all_contracts) - n_0dte

                strikes_0dte = sorted(set(c['strike'] for c in all_contracts if c.get('is_0dte')))
                ts = datetime.now().strftime('%H:%M:%S')
                print(f'[{ts}] Watch list: {len(all_contracts)} contracts  '
                      f'(0DTE={n_0dte}  MULTI={n_multi})  '
                      f'0DTE range {int(strikes_0dte[0]) if strikes_0dte else "?"}'
                      f'–{int(strikes_0dte[-1]) if strikes_0dte else "?"}')
                _streamer.update_options_watch(all_contracts)

        gex_b      = total_gex / 1e9
        regime_str = 'POS' if total_gex > 0 else 'NEG'
        flip  = levels_multi.get('flip_level')
        pw    = levels_multi.get('put_wall')
        cw    = levels_multi.get('call_wall')
        pin   = levels_multi.get('pin')
        gex_0dte_b = float(gex_0dte['net_gex'].sum()) / 1e9 if not gex_0dte.empty else 0.0
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] GEX  {symbol.replace('$','').replace('/',''):4s}"
            f"  spot={spot:>8.2f}"
            f"  total={gex_b:+.2f}B ({regime_str})"
            f"  0dte={gex_0dte_b:+.2f}B"
            f"  flip={flip}  pw={pw}  cw={cw}  pin={pin}"
            f"  |  {len(watch)} contracts watched"
        )

    except Exception as e:
        print(f"[ERROR] GEX {symbol}: {e}")


# ── Price history ───────────────────────────────────────────────────────────────

def refresh_price(symbol: str):
    """
    Fetch the latest candles for a symbol and merge into the CSV + in-memory cache.

    Schwab's period=N only covers *completed* trading sessions — it will NOT
    return today's live candles.  To pick up the current session we do two fetches:
      1. period=2  → catches up any missed completed sessions (e.g. after a gap)
      2. startDate=today_midnight … endDate=now+1h  → today's live intraday bars

    The CSV deduplicates by datetime so double-fetching yesterday's data is harmless.
    """
    try:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo('America/New_York')

        token = get_access_token()
        total_added = 0

        # ── Pass 1: completed sessions (catches multi-day gaps after weekends) ──
        historical = fetch_candles(symbol, token, days=2, frequency=1)
        if historical:
            with csv_lock:
                added = append_candles(symbol, historical)
            total_added += added

        # ── Pass 2: current live session (period= never includes today) ──────
        today_midnight_et = datetime.now(tz=ET).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start_ms = int(today_midnight_et.timestamp() * 1000)
        end_ms   = int((time.time() + 3600) * 1000)   # now + 1h buffer
        live = fetch_candles(symbol, token, frequency=1, start_ms=start_ms, end_ms=end_ms)
        if live:
            with csv_lock:
                added = append_candles(symbol, live)
            total_added += added
            if added > 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Price live: {symbol} +{added} candles today")

        if total_added > 0:
            candles = load_candles(symbol)
            with cache_lock:
                candle_cache[symbol] = candles
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Price updated: {symbol} +{total_added} total")

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
        candles = list(candle_cache.get(key, []))

    # ES futures trade nearly 24h; cap to the last 2 days so we don't return
    # years of accumulated CSV history (~694K rows) to the browser.
    # Equity/ETF symbols: filter to regular market hours (9:30–16:00 ET) and
    # return ONLY the most recent trading session so yesterday's data does not
    # appear as a stale backdrop behind today's price action.
    if symbol == 'ES':
        cutoff_ms = (time.time() - 2 * 86400) * 1000
        candles = [c for c in candles if c['datetime'] >= cutoff_ms]
    else:
        market_open  = dtime(9, 30)
        market_close = dtime(16, 0)

        # Collect all market-hours candles, grouped by ET date
        by_date: dict = {}
        for c in candles:
            dt = datetime.fromtimestamp(c['datetime'] / 1000, tz=ET)
            if market_open <= dt.time() <= market_close:
                by_date.setdefault(dt.date(), []).append(c)

        if by_date:
            # Return the 2 most recent trading sessions
            recent_dates = sorted(by_date.keys())[-2:]
            candles = []
            for d in recent_dates:
                candles.extend(by_date[d])
        else:
            candles = []

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


@app.route('/api/debug/price/<symbol>')
def api_debug_price(symbol):
    """Diagnostic endpoint — returns raw candle_cache sample to verify server-side data."""
    from zoneinfo import ZoneInfo
    ET = ZoneInfo('America/New_York')
    key = f'${symbol}' if symbol == 'SPX' else f'/{symbol}' if symbol == 'ES' else symbol
    with cache_lock:
        candles = list(candle_cache.get(key, []))

    if not candles:
        return jsonify({'error': 'no data', 'key': key})

    def fmt(c):
        dt = datetime.fromtimestamp(c['datetime'] / 1000, tz=ET)
        return {
            'dt_et':    dt.strftime('%Y-%m-%d %H:%M'),
            'datetime': c['datetime'],
            'time_s':   c['datetime'] // 1000,
            'open':     c['open'],
            'high':     c['high'],
            'low':      c['low'],
            'close':    c['close'],
            'volume':   c['volume'],
        }

    bodies = [abs(c['close'] - c['open']) for c in candles]
    ranges = [c['high'] - c['low'] for c in candles]
    return jsonify({
        'key':           key,
        'total_candles': len(candles),
        'first_3':       [fmt(c) for c in candles[:3]],
        'last_3':        [fmt(c) for c in candles[-3:]],
        'stats': {
            'avg_body':  round(sum(bodies) / len(bodies), 4) if bodies else 0,
            'max_body':  round(max(bodies), 4) if bodies else 0,
            'avg_range': round(sum(ranges) / len(ranges), 4) if ranges else 0,
            'price_min': round(min(c['low']  for c in candles), 2),
            'price_max': round(max(c['high'] for c in candles), 2),
            'vol_min':   min(c['volume'] for c in candles),
            'vol_max':   max(c['volume'] for c in candles),
        }
    })


@app.route('/api/flow_alerts')
def api_flow_alerts():
    """Return today's flow alerts for page load restore."""
    with _flow_alerts_lock:
        return jsonify(list(_flow_alerts))


@app.route('/api/delta_flow')
def api_delta_flow():
    """Return all-expiry cumulative delta series for the current session."""
    return jsonify(delta_flow.get_series())


@app.route('/api/delta_flow/0dte')
def api_delta_flow_0dte():
    """Return 0DTE-only cumulative delta series for the current session."""
    return jsonify(delta_flow.get_series_0dte())


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
    Updates candle_cache, pushes to SSE clients, and persists completed
    bars to CSV immediately (is_final=True = CHART_* completed bar).
    candle: { symbol, datetime (ms), open, high, low, close, volume, is_final }
    """
    raw_symbol = candle['symbol']
    cache_key  = raw_symbol
    ts_ms      = candle['datetime']
    is_final   = candle.get('is_final', False)

    # Update candle cache — only for current or newer candles.
    # CHART_EQUITY sends a history dump on subscribe; those old bars must NOT be
    # appended (they already exist in the cache from the CSV) or the cache gets
    # duplicates, which corrupts LW Charts setData() and causes tall green bars.
    push_sse   = False
    write_csv  = False
    with cache_lock:
        existing = candle_cache.get(cache_key, [])
        if existing:
            last_ts = existing[-1]['datetime']
            if ts_ms == last_ts:
                existing[-1] = candle   # update the in-progress candle
                push_sse = True
            elif ts_ms > last_ts:
                existing.append(candle) # genuinely new bar
                push_sse  = True
                write_csv = is_final    # only persist completed bars
            # else: historical bar older than last cached — skip silently
        else:
            existing.append(candle)
            push_sse  = True
            write_csv = is_final
        candle_cache[cache_key] = existing

    if not push_sse:
        return   # stale history bar — don't corrupt the live chart

    # Persist completed bar to CSV immediately so it survives a restart.
    # append_candles deduplicates by datetime — safe if REST poll also writes it.
    if write_csv:
        csv_candle = {k: v for k, v in candle.items() if k not in ('is_final', 'symbol')}
        try:
            with csv_lock:
                append_candles(raw_symbol, [csv_candle])
        except Exception as e:
            print(f'[ERROR] CSV write {raw_symbol}: {e}')

    # Strip is_final before pushing to browser — JS doesn't need it.
    # Normalize symbol to display form (strip $ and /) so the frontend
    # chart key matches: '/ES' → 'ES', '$SPX' → 'SPX', 'SPY' → 'SPY'.
    display_sym = raw_symbol.replace('$', '').replace('/', '')
    sse_push({
        'type':    'candle',
        'symbol':  display_sym,
        'time':    ts_ms // 1000,   # ms → seconds
        'open':    candle['open'],
        'high':    candle['high'],
        'low':     candle['low'],
        'close':   candle['close'],
        'volume':  candle['volume'],
    })

    ts = datetime.now().strftime('%H:%M:%S')
    flag = ' [saved]' if write_csv else ''
    print(f'[{ts}] Streamer candle: {raw_symbol} {candle["close"]:.2f}{flag}')



def on_flow_alert(alert: dict):
    alert['time'] = int(datetime.now().timestamp())
    sse_push({'type': 'flow_alert', **alert})
    delta_flow.record_alert(alert)
    with _flow_alerts_lock:
        _flow_alerts.append(alert)
        _save_flow_alerts()
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] FLOW  {alert['underlying']} {alert['strike']} "
          f"{alert['side'].upper()} {alert['expiry_label']}  "
          f"+{alert['volume_delta']:,} contracts  {alert.get('direction','?')}")


if __name__ == '__main__':
    setup_logging()
    print("Starting GEX Dashboard...")
    print("\nSyncing price history (gap-fill)...")
    token = get_access_token()
    for symbol in PRICE_SYMBOLS:
        try:
            candles = sync_symbol(symbol, token)
            # Cap ES in-memory cache to last 5 days — full CSV has 700K+ rows
            if symbol == '/ES':
                cutoff_ms = (time.time() - 5 * 86400) * 1000
                candles = [c for c in candles if c['datetime'] >= cutoff_ms]
            with cache_lock:
                candle_cache[symbol] = candles
            print(f"  {symbol}: {len(candles)} candles loaded into cache")
        except Exception as e:
            print(f"  [ERROR] {symbol} sync failed: {e}")
    delta_flow.set_sse_push(sse_push)
    delta_flow.load_today()
    load_flow_alerts_today()
    print("\nFetching initial GEX data...")
    for symbol in SYMBOLS:
        refresh_gex(symbol)
    threading.Thread(target=gex_loop, daemon=True).start()
    threading.Thread(target=price_loop, daemon=True).start()
    _streamer = SchwabStreamer(on_candle=on_streamer_candle, on_flow_alert=on_flow_alert)
    _streamer.start()
    print("\nDashboard running at http://127.0.0.1:5000")
    try:
        app.run(debug=False, port=5000, threaded=True)
    except Exception as e:
        import traceback
        print(f"[FATAL] Flask crashed: {e}")
        traceback.print_exc()
        input("Press Enter to exit...")
