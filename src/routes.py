"""
routes.py — Flask API route handlers.

All @app.route endpoints. Registered on the app via register(app) called
from dashboard.py at startup. Reads from shared cache dicts injected via init().

Public API:
    init(cache, candle_cache, cache_lock)  — inject shared state at startup
    register(app)                          — register all routes on the Flask app
"""

import time
from datetime import datetime
from queue import Queue, Empty
from zoneinfo import ZoneInfo
from flask import jsonify, render_template, Response, stream_with_context

import delta_flow
import flow_alerts
import sse

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

    @app.route('/api/price/<symbol>')
    def api_price(symbol):
        from datetime import time as dtime
        key = f'${symbol}' if symbol == 'SPX' else f'/{symbol}' if symbol == 'ES' else symbol
        with _cache_lock:
            candles = list(_candle_cache.get(key, []))

        if symbol == 'ES':
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
        return jsonify(flow_alerts.get_all())

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
