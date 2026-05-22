"""
Schwab WebSocket streamer — real-time candle updates + options flow monitoring.

Subscriptions:
  CHART_EQUITY      → SPY, QQQ        (1-min OHLCV candles, bar boundaries)
  CHART_FUTURES     → /ES             (1-min OHLCV candles, bar boundaries)
  LEVELONE_EQUITIES → SPY, QQQ        (tick-by-tick last price — animates current candle)
  LEVELONE_FUTURES  → /ES             (tick-by-tick last price — animates current candle)
  LEVELONE_OPTIONS  → call-wall strikes (volume spike / flow alerts)

Callbacks:
  on_candle(dict)      — new/updated 1-min price candle (fires on every tick)
  on_flow_alert(dict)  — weighted options volume spike detected

Usage:
    streamer = SchwabStreamer(on_candle=cb, on_flow_alert=flow_cb)
    streamer.start()
    streamer.update_options_watch(contracts)   # call after each GEX refresh
    streamer.stop()
"""

import json
import time
import threading
import requests
import websocket
from datetime import datetime

from gex import get_access_token

# ── Price streaming ─────────────────────────────────────────────────────────────
STREAM_EQUITY  = ['SPY', 'QQQ']
STREAM_FUTURES = ['/ES']
CHART_FIELDS        = '0,1,2,3,4,5,6,7,8'
LEVELONE_FIELDS     = '3,8'    # field 3 = last price, field 8 = total volume

# ── Options flow ────────────────────────────────────────────────────────────────
# LEVELONE_OPTIONS fields we care about:
#   0 = key (contract symbol)   2 = bid   3 = ask   4 = last   8 = total volume
OPTIONS_FIELDS   = '0,2,3,4,8'
ALERT_THRESHOLD  = 300    # weighted contracts to trigger a flow alert
                          # 0DTE weight=1.0 → 300 raw contracts
                          # MULTI weight=0.15 → ~2000 raw contracts


def get_streamer_info(access_token: str) -> dict:
    """Fetch WebSocket URL and session credentials from Schwab user preferences."""
    resp = requests.get(
        'https://api.schwabapi.com/trader/v1/userPreference',
        headers={'Authorization': f'Bearer {access_token}'}
    )
    if not resp.ok:
        print(f'[STREAMER] userPreference {resp.status_code}: {resp.text}')
    resp.raise_for_status()
    info = resp.json()['streamerInfo'][0]
    return {
        'url':         info['streamerSocketUrl'],
        'customer_id': info['schwabClientCustomerId'],
        'correl_id':   info['schwabClientCorrelId'],
        'channel':     info.get('schwabClientChannel',    'N9'),
        'function_id': info.get('schwabClientFunctionId', 'APIAPP'),
    }


def _make_request(service, command, req_id, customer_id, correl_id, parameters):
    return json.dumps({
        'requests': [{
            'service':                service,
            'command':                command,
            'requestid':              str(req_id),
            'SchwabClientCustomerId': customer_id,
            'SchwabClientCorrelId':   correl_id,
            'parameters':             parameters,
        }]
    })


def _parse_chart(msg: dict) -> list:
    """Parse CHART_EQUITY / CHART_FUTURES content into candle dicts."""
    candles = []
    for item in msg.get('content', []):
        ts_ms = item.get('7')
        if ts_ms is None:
            continue
        candles.append({
            'symbol':   item.get('key', ''),
            'datetime': int(ts_ms),
            'open':     float(item.get('1', 0)),
            'high':     float(item.get('2', 0)),
            'low':      float(item.get('3', 0)),
            'close':    float(item.get('4', 0)),
            'volume':   int(item.get('5', 0)),
        })
    return candles


def _floor_minute_ms() -> int:
    """Return the current minute boundary as a Unix millisecond timestamp."""
    return int(time.time() // 60) * 60 * 1000


def _parse_levelone(msg: dict) -> list:
    """Parse LEVELONE_EQUITIES / LEVELONE_FUTURES into tick dicts."""
    ticks = []
    for item in msg.get('content', []):
        last = item.get('3')
        if last is None:
            continue
        ticks.append({
            'symbol': item.get('key', ''),
            'last':   float(last),
            'volume': int(item.get('8', 0)),
        })
    return ticks


def _parse_options(msg: dict) -> list:
    """Parse LEVELONE_OPTIONS content into quote dicts."""
    quotes = []
    for item in msg.get('content', []):
        symbol = item.get('key', '')
        vol    = item.get('8')
        if vol is None:
            continue
        quotes.append({
            'symbol': symbol,
            'bid':    float(item.get('2', 0)),
            'ask':    float(item.get('3', 0)),
            'last':   float(item.get('4', 0)),
            'volume': int(vol),
        })
    return quotes


class SchwabStreamer:
    """
    Persistent Schwab WebSocket streamer with automatic reconnection.
    Handles price candles and options flow monitoring in a single connection.
    """

    def __init__(self, on_candle, on_flow_alert=None):
        self.on_candle      = on_candle
        self.on_flow_alert  = on_flow_alert

        self._running  = False
        self._ws       = None
        self._thread   = None

        # Live candle state — one per symbol, updated on every tick
        # Used to animate the current 1-min bar between CHART_EQUITY bar boundaries
        self._live_candles   = {}    # symbol -> { symbol, datetime (ms), open, high, low, close, volume }

        # Options watch list — set via update_options_watch()
        # contract_info: symbol -> { strike, side, expiry_label, weight, underlying }
        self._watch_lock     = threading.Lock()
        self._contract_info  = {}    # symbol -> metadata dict
        self._volume_cache   = {}    # symbol -> last known day volume
        self._pending_update = False # flag: watch list changed, need re-subscribe

    # ── Public API ──────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run_loop, daemon=True,
                                         name='schwab-streamer')
        self._thread.start()
        print('[STREAMER] Background thread started.')

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def update_options_watch(self, contracts: list):
        """
        Replace the options watch list with a new set of contracts.
        Contracts is a list of dicts from get_watch_contracts():
          { symbol, strike, side, expiry_label, weight, is_0dte, underlying? }
        Thread-safe — safe to call from any thread.
        """
        with self._watch_lock:
            self._contract_info = {
                c['symbol']: c for c in contracts
            }
            self._volume_cache   = {}   # reset volume baseline on new watch list
            self._pending_update = True

        syms = [c['symbol'] for c in contracts]
        print(f'[STREAMER] Options watch updated: {len(syms)} contracts')

    # ── Internal ────────────────────────────────────────────────────────────────

    def _run_loop(self):
        backoff = 5
        while self._running:
            try:
                self._connect()
                backoff = 5
            except Exception as exc:
                if not self._running:
                    break
                print(f'[STREAMER] Error: {exc}. Retrying in {backoff}s...')
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def _connect(self):
        print('[STREAMER] Fetching access token...')
        token = get_access_token()
        info  = get_streamer_info(token)
        cid, corr = info['customer_id'], info['correl_id']
        print(f"[STREAMER] Connecting → {info['url']}")

        def on_open(ws):
            print('[STREAMER] Connected. Sending LOGIN...')
            ws.send(_make_request('ADMIN', 'LOGIN', 0, cid, corr, {
                'Authorization':          token,
                'SchwabClientChannel':    info['channel'],
                'SchwabClientFunctionId': info['function_id'],
            }))

        def on_message(ws, raw):
            try:
                data = json.loads(raw)
            except Exception:
                return

            # LOGIN response → subscribe after success
            for resp in data.get('response', []):
                if resp.get('service') == 'ADMIN' and resp.get('command') == 'LOGIN':
                    code = resp.get('content', {}).get('code', -1)
                    if code == 0:
                        print('[STREAMER] Login OK. Subscribing...')
                        self._subscribe_price(ws, cid, corr)
                        self._subscribe_options(ws, cid, corr)
                        self._pending_update = False
                    else:
                        print(f'[STREAMER] Login FAILED code={code}')

            # Price candle data
            for notify in data.get('data', []):
                service = notify.get('service', '')
                if service in ('CHART_EQUITY', 'CHART_FUTURES'):
                    # Official completed bar — sync live candle and fire callback
                    for candle in _parse_chart(notify):
                        self._live_candles[candle['symbol']] = dict(candle)
                        try:
                            self.on_candle(candle)
                        except Exception as exc:
                            print(f'[STREAMER] on_candle error: {exc}')

                elif service in ('LEVELONE_EQUITIES', 'LEVELONE_FUTURES'):
                    for tick in _parse_levelone(notify):
                        try:
                            self._handle_tick(tick)
                        except Exception as exc:
                            print(f'[STREAMER] tick error: {exc}')

                elif service == 'LEVELONE_OPTIONS':
                    for quote in _parse_options(notify):
                        try:
                            self._handle_options_quote(quote)
                        except Exception as exc:
                            print(f'[STREAMER] flow error: {exc}')

            # Re-subscribe options if watch list changed while connected
            if self._pending_update and self._ws:
                self._subscribe_options(ws, cid, corr)
                self._pending_update = False

        def on_error(ws, error):
            print(f'[STREAMER] WS error: {error}')

        def on_close(ws, code, msg):
            print(f'[STREAMER] Connection closed (code={code})')

        self._ws = websocket.WebSocketApp(
            info['url'],
            on_open=on_open, on_message=on_message,
            on_error=on_error, on_close=on_close,
        )
        self._ws.run_forever(ping_interval=25, ping_timeout=10)

    def _subscribe_price(self, ws, cid, corr):
        ws.send(_make_request('CHART_EQUITY',      'SUBS', 1, cid, corr,
                              {'keys': ','.join(STREAM_EQUITY),  'fields': CHART_FIELDS}))
        ws.send(_make_request('CHART_FUTURES',     'SUBS', 2, cid, corr,
                              {'keys': ','.join(STREAM_FUTURES), 'fields': CHART_FIELDS}))
        ws.send(_make_request('LEVELONE_EQUITIES', 'SUBS', 4, cid, corr,
                              {'keys': ','.join(STREAM_EQUITY),  'fields': LEVELONE_FIELDS}))
        ws.send(_make_request('LEVELONE_FUTURES',  'SUBS', 5, cid, corr,
                              {'keys': ','.join(STREAM_FUTURES), 'fields': LEVELONE_FIELDS}))
        print(f'[STREAMER] Subscribed CHART + LEVELONE: {STREAM_EQUITY + STREAM_FUTURES}')

    def _subscribe_options(self, ws, cid, corr):
        with self._watch_lock:
            symbols = list(self._contract_info.keys())
        if not symbols:
            return
        ws.send(_make_request('LEVELONE_OPTIONS', 'SUBS', 3, cid, corr,
                              {'keys': ','.join(symbols), 'fields': OPTIONS_FIELDS}))
        print(f'[STREAMER] Subscribed LEVELONE_OPTIONS: {len(symbols)} contracts')

    def _handle_tick(self, tick: dict):
        """
        Update the live in-progress candle for a symbol on each price tick.
        Fires on_candle so the browser updates the current bar in real time.
        """
        symbol = tick['symbol']
        last   = tick['last']
        now_ms = _floor_minute_ms()

        live = self._live_candles.get(symbol)

        if live is None or live['datetime'] != now_ms:
            # New minute — start a fresh candle; open from prev close if available
            open_price = live['close'] if live else last
            self._live_candles[symbol] = {
                'symbol':   symbol,
                'datetime': now_ms,
                'open':     open_price,
                'high':     last,
                'low':      last,
                'close':    last,
                'volume':   tick['volume'],
            }
        else:
            live['close']  = last
            live['high']   = max(live['high'], last)
            live['low']    = min(live['low'],  last)
            live['volume'] = tick['volume']

        try:
            self.on_candle(dict(self._live_candles[symbol]))
        except Exception as exc:
            print(f'[STREAMER] on_candle error: {exc}')

    def _handle_options_quote(self, quote: dict):
        """
        Compare incoming volume to cached baseline.
        Fire alert when weighted volume delta >= ALERT_THRESHOLD.
        """
        symbol  = quote['symbol']

        with self._watch_lock:
            meta = self._contract_info.get(symbol)
            if meta is None:
                return   # not in our watch list
            prev_vol = self._volume_cache.get(symbol)
            self._volume_cache[symbol] = quote['volume']

        if prev_vol is None:
            return   # first data point — establish baseline, don't alert yet

        delta = quote['volume'] - prev_vol
        if delta <= 0:
            return

        weighted = delta * meta['weight']
        if weighted < ALERT_THRESHOLD:
            return

        # Infer direction: last >= midpoint → bought at ask (bullish), else sold (bearish)
        mid       = (quote['bid'] + quote['ask']) / 2 if quote['ask'] > 0 else 0
        direction = 'BUY' if quote['last'] >= mid else 'SELL'

        # Underlying symbol — strip option suffix (e.g. "SPY   260522C..." → "SPY")
        underlying = meta.get('underlying') or symbol[:3].strip()

        alert = {
            'symbol':         symbol,
            'underlying':     underlying,
            'strike':         meta['strike'],
            'side':           meta['side'],           # 'call' | 'put'
            'expiry_label':   meta['expiry_label'],   # '0DTE' | 'MULTI'
            'is_0dte':        meta['is_0dte'],
            'weight':         meta['weight'],
            'volume_delta':   delta,
            'weighted_delta': round(weighted, 1),
            'last':           quote['last'],
            'bid':            quote['bid'],
            'ask':            quote['ask'],
            'direction':      direction,
            'time':           datetime.now().strftime('%H:%M:%S'),
        }

        if self.on_flow_alert:
            self.on_flow_alert(alert)
