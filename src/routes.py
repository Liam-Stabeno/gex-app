"""
routes.py — Flask API route handlers.

All @app.route endpoints. Registered on the app via register(app) called
from dashboard.py at startup. Reads from shared cache dicts injected via init().

Public API:
    init(cache, candle_cache, cache_lock)  — inject shared state at startup
    register(app)                          — register all routes on the Flask app
"""

import os
import time
from datetime import datetime
from queue import Queue, Empty
from zoneinfo import ZoneInfo
from flask import request, jsonify, render_template, Response, stream_with_context

import delta_flow
import flow_alerts
import sse
import tos_rtd

ET = ZoneInfo('America/New_York')

# Shared state — injected via init()
_cache        = None
_candle_cache = None
_cache_lock   = None


def init(cache, candle_cache, cache_lock):
    global _cache, _candle_cache, _cache_lock
    _cache        = cache
    _candle_cache = candle_cache
    _cache_lock   = cache_lock


def register(app):
    """Attach all routes to the Flask app instance."""

    def _req_day():
        """?date=YYYY-MM-DD -> date, None when absent; raises ValueError when malformed."""
        d = request.args.get('date')
        return datetime.strptime(d, '%Y-%m-%d').date() if d else None

    @app.route('/api/history_days')
    def api_history_days():
        """Days with saved data for replay, newest first."""
        import gex_stats
        return jsonify(gex_stats.history_days('SPX'))

    @app.route('/api/heatmap_reliability/<symbol>')
    def api_heatmap_reliability(symbol):
        """How the heatmap's bright bands did per gamma regime, from the scorecard."""
        import gex_stats
        return jsonify(gex_stats.heatmap_reliability(symbol.upper().replace('$', '')))

    @app.route('/')
    def index():
        return render_template('dashboard.html')

    @app.route('/api/gex/<symbol>')
    def api_gex(symbol):
        key = f'${symbol}' if symbol == 'SPX' else f'/{symbol}' if symbol == 'ES' else symbol
        with _cache_lock:
            data = _cache.get(key) or _cache.get(symbol)
        if not data:
            return jsonify({'error': 'No data yet'}), 202
        return jsonify(data)

    @app.route('/api/volume_split/<symbol>')
    def api_volume_split(symbol):
        """Futures buy/sell volume per minute (tick rule), today and the previous day."""
        from background import load_volume_split
        try:
            day = _req_day()
        except ValueError:
            return jsonify({'error': 'date must be YYYY-MM-DD'}), 400
        return jsonify(load_volume_split(symbol.upper().replace('/', ''), day=day))

    @app.route('/api/gex_heatmap/<symbol>')
    def api_gex_heatmap(symbol):
        """Today's net GEX by strike over time (5-min snapshots) for the gamma-zone heatmap."""
        import gex_stats
        day = None
        if request.args.get('date'):
            try:
                day = datetime.strptime(request.args['date'], '%Y-%m-%d').date()
            except ValueError:
                return jsonify({'error': 'date must be YYYY-MM-DD'}), 400
        return jsonify(gex_stats.load_heatmap(symbol.upper().replace('$', ''), day=day))

    @app.route('/api/gamma_levels/<symbol>')
    def api_gamma_levels(symbol):
        """Walls, trapdoor, squeeze, support/resistance, air pockets, brakes at spot."""
        import gex_stats
        mode = request.args.get('mode', 'all')
        try:
            day = _req_day()
        except ValueError:
            return jsonify({'error': 'date must be YYYY-MM-DD'}), 400
        if day is not None and day != datetime.now(ET).date():      # replay: that day's own spot
            return jsonify(gex_stats.current_gamma_levels(None, mode, symbol.upper().replace('$', ''), day=day))
        key = f'${symbol}' if symbol == 'SPX' else symbol
        with _cache_lock:
            spot = (_cache.get(key) or {}).get('spot')
        if not spot:
            return jsonify(None)
        return jsonify(gex_stats.current_gamma_levels(float(spot), mode, symbol.upper().replace('$', '')))

    @app.route('/api/expected_move/<symbol>')
    def api_expected_move(symbol):
        """Typical distance to the close for the current GEX regime, from saved history."""
        import gex_stats
        key = f'${symbol}' if symbol == 'SPX' else symbol
        with _cache_lock:
            data = _cache.get(key) or {}
        if data.get('total_gex') is None or data.get('last_session'):
            return jsonify(None)
        return jsonify(gex_stats.expected_move_now(data['total_gex']))

    @app.route('/api/gex_levels/<symbol>')
    def api_gex_levels(symbol):
        """Today's level history (flip, walls, pin, true pin) for the trail charts."""
        from background import load_level_history
        try:
            day = _req_day()
        except ValueError:
            return jsonify({'error': 'date must be YYYY-MM-DD'}), 400
        return jsonify(load_level_history(symbol.upper().replace('$', ''), day))

    @app.route('/api/price/<symbol>')
    def api_price(symbol):
        from datetime import time as dtime
        key = f'${symbol}' if symbol == 'SPX' else f'/{symbol}' if symbol == 'ES' else symbol
        with _cache_lock:
            candles = list(_candle_cache.get(key, []))

        try:
            day = _req_day()
        except ValueError:
            return jsonify({'error': 'date must be YYYY-MM-DD'}), 400
        if day is not None:
            from datetime import timedelta
            start_ms = int(datetime.combine(day, dtime(0, 0), ET).timestamp() * 1000)
            if not candles or candles[0]['datetime'] > start_ms:
                # older than the in-memory window: read just that day from the file
                from price_history import load_range
                end_ms = int(datetime.combine(day + timedelta(days=1), dtime(0, 0), ET).timestamp() * 1000)
                candles = load_range(key, start_ms, end_ms)
            sel = []
            for c in candles:
                dt = datetime.fromtimestamp(c['datetime'] / 1000, tz=ET)
                if dt.date() == day and (symbol == 'ES' or dtime(9, 30) <= dt.time() <= dtime(16, 0)):
                    sel.append(c)
            candles = sel
        elif symbol == 'ES':
            cutoff_ms = (time.time() - 2 * 86400) * 1000
            candles = [c for c in candles if c['datetime'] >= cutoff_ms]
        else:
            market_open  = dtime(9, 30)
            market_close = dtime(16, 0)
            by_date: dict = {}
            for c in candles:
                dt = datetime.fromtimestamp(c['datetime'] / 1000, tz=ET)
                if market_open <= dt.time() <= market_close:
                    by_date.setdefault(dt.date(), []).append(c)
            if by_date:
                recent_dates = sorted(by_date.keys())[-2:]
                candles = []
                for d in recent_dates:
                    candles.extend(by_date[d])
            else:
                candles = []

        return jsonify([{
            'time':   int(c['datetime'] / 1000),
            'open':   c['open'],
            'high':   c['high'],
            'low':    c['low'],
            'close':  c['close'],
            'volume': c['volume'],
        } for c in candles])

    @app.route('/api/all')
    def api_all():
        with _cache_lock:
            return jsonify(list(_cache.values()))

    @app.route('/api/rtd/status')
    def api_rtd_status():
        return jsonify(tos_rtd.get_status())

    @app.route('/api/rtd/toggle', methods=['POST'])
    def api_rtd_toggle():
        return jsonify(tos_rtd.toggle())

    @app.route('/api/rtd/quotes')
    def api_rtd_quotes():
        """Debug: show all live RTD quotes (bid/ask per TOS symbol)."""
        with tos_rtd._quotes_lock:
            snapshot = dict(tos_rtd._quotes)
        rows = []
        for sym in sorted(snapshot):
            q = snapshot[sym]
            bid = q.get('bid', 0.0)
            ask = q.get('ask', 0.0)
            rows.append({'symbol': sym, 'bid': bid, 'ask': ask,
                         'mid': round((bid + ask) / 2, 4) if bid and ask else None})
        return jsonify({'count': len(rows), 'quotes': rows,
                        'status': tos_rtd.get_status()})

    @app.route('/api/debug/chain')
    def api_debug_chain():
        """Fetch a fresh SPX chain and report gamma stats — for diagnosing GEX=0."""
        from gex import get_access_token, fetch_option_chain
        token = get_access_token()
        chain = fetch_option_chain('$SPX', token, strike_count=10)
        spot  = chain.get('underlyingPrice')
        # Sample first 3 options from first expiration in callExpDateMap
        samples = []
        for exp_key, strikes in list(chain.get('callExpDateMap', {}).items())[:2]:
            for strike_str, opts in list(strikes.items())[:3]:
                if opts:
                    o = opts[0]
                    samples.append({
                        'exp': exp_key,
                        'strike': strike_str,
                        'gamma': o.get('gamma'),
                        'delta': o.get('delta'),
                        'oi':    o.get('openInterest'),
                        'bid':   o.get('bid'),
                        'ask':   o.get('ask'),
                    })
        return jsonify({'spot': spot, 'samples': samples,
                        'exp_keys': list(chain.get('callExpDateMap', {}).keys())[:10]})

    @app.route('/api/debug/price/<symbol>')
    def api_debug_price(symbol):
        """Diagnostic — returns raw candle_cache sample."""
        key = f'${symbol}' if symbol == 'SPX' else f'/{symbol}' if symbol == 'ES' else symbol
        with _cache_lock:
            candles = list(_candle_cache.get(key, []))
        if not candles:
            return jsonify({'error': 'no data', 'key': key})

        def fmt(c):
            dt = datetime.fromtimestamp(c['datetime'] / 1000, tz=ET)
            return {
                'dt_et':    dt.strftime('%Y-%m-%d %H:%M'),
                'datetime': c['datetime'],
                'time_s':   c['datetime'] // 1000,
                'open':     c['open'],  'high': c['high'],
                'low':      c['low'],   'close': c['close'],
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
            },
        })

    @app.route('/api/flow_alerts')
    def api_flow_alerts():
        try:
            day = _req_day()
        except ValueError:
            return jsonify({'error': 'date must be YYYY-MM-DD'}), 400
        if day is None or day == datetime.now(ET).date():
            return jsonify(flow_alerts.get_all())
        import json as _json
        path = flow_alerts._path(day.isoformat())
        if not os.path.exists(path):
            return jsonify([])
        with open(path) as f:
            return jsonify(_json.load(f))

    @app.route('/api/delta_flow')
    def api_delta_flow():
        return jsonify(delta_flow.get_series())

    @app.route('/api/delta_flow/0dte')
    def api_delta_flow_0dte():
        return jsonify(delta_flow.get_series_0dte())

    @app.route('/api/stream')
    def api_stream():
        q = Queue(maxsize=200)
        with sse.lock:
            sse.clients.append(q)

        def generate():
            try:
                yield 'data: {"type":"connected"}\n\n'
                while True:
                    try:
                        msg = q.get(timeout=25)
                        yield msg
                    except Empty:
                        yield ': keepalive\n\n'
            finally:
                with sse.lock:
                    if q in sse.clients:
                        sse.clients.remove(q)

        return Response(
            stream_with_context(generate()),
            content_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )
