"""
delta_flow.py — Cumulative dealer delta tracking for SPX options.

Tracks signed delta-notional pressure from two sources:
  1. Full-book chain diff (every 60s REST refresh) — covers ALL SPX strikes
  2. Real-time streaming alerts — catches large prints between refreshes

Two series are maintained in parallel:
  • All expiries (up to 60 DTE) — full picture of dealer hedging
  • 0DTE only — same-day expiry contracts only; most reactive to price

Dealer hedge sign convention:
  Call BUY  → dealer short call  → buys futures  → +delta
  Call SELL → dealer long call   → sells futures → -delta
  Put  BUY  → dealer short put   → sells futures → -delta
  Put  SELL → dealer long put    → buys futures  → +delta

Notional = contracts × strike × $100 multiplier × real_delta
Resets daily at 09:30 ET.  Persisted to data/delta_flow_YYYY-MM-DD.json.
"""

import os
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

# Resolve data/ relative to project root (one level up from src/)
_SRC_DIR     = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SRC_DIR)
_DATA_DIR    = os.path.join(_PROJECT_DIR, 'data')

# ── State ────────────────────────────────────────────────────────────────────────

_delta_flow_lock = threading.Lock()

# All expiries
_delta_flow: dict = {
    'call':     [],    # [{time (unix s), value (cumulative USD)}]
    'put':      [],
    'call_cum': 0.0,
    'put_cum':  0.0,
    'day':      None,
}

# 0DTE only
_delta_flow_0dte: dict = {
    'call':     [],
    'put':      [],
    'call_cum': 0.0,
    'put_cum':  0.0,
    'day':      None,
}

# Previous chain snapshot for volume diffing
_spx_chain_prev: dict = {}
_spx_chain_lock = threading.Lock()

# SSE push — injected at startup to avoid circular import
_sse_push = None

def set_sse_push(fn):
    """Inject the SSE push function. Called from dashboard.py at startup."""
    global _sse_push
    _sse_push = fn


# ── Persistence ──────────────────────────────────────────────────────────────────

def _delta_flow_path(date_obj) -> str:
    return os.path.join(_DATA_DIR, f'delta_flow_{date_obj}.json')


def _save_delta_flow():
    """Write both series to today's JSON file. Must be called inside _delta_flow_lock."""
    if _delta_flow['day'] is None:
        return
    path = _delta_flow_path(_delta_flow['day'])
    try:
        with open(path, 'w') as f:
            json.dump({
                'call':          _delta_flow['call'],
                'put':           _delta_flow['put'],
                'call_cum':      _delta_flow['call_cum'],
                'put_cum':       _delta_flow['put_cum'],
                'day':           str(_delta_flow['day']),
                '0dte_call':     _delta_flow_0dte['call'],
                '0dte_put':      _delta_flow_0dte['put'],
                '0dte_call_cum': _delta_flow_0dte['call_cum'],
                '0dte_put_cum':  _delta_flow_0dte['put_cum'],
            }, f)
    except Exception as e:
        print(f'[ERROR] delta_flow save: {e}')


def load_today():
    """Load today's persisted delta flow on startup, if it exists."""
    today = datetime.now(tz=ET).date()
    path  = _delta_flow_path(today)
    if not os.path.exists(path):
        print('[delta_flow] No saved session for today — starting fresh')
        return
    try:
        with open(path) as f:
            saved = json.load(f)
        with _delta_flow_lock:
            _delta_flow['call']          = saved.get('call', [])
            _delta_flow['put']           = saved.get('put', [])
            _delta_flow['call_cum']      = saved.get('call_cum', 0.0)
            _delta_flow['put_cum']       = saved.get('put_cum',  0.0)
            _delta_flow['day']           = today
            _delta_flow_0dte['call']     = saved.get('0dte_call', [])
            _delta_flow_0dte['put']      = saved.get('0dte_put', [])
            _delta_flow_0dte['call_cum'] = saved.get('0dte_call_cum', 0.0)
            _delta_flow_0dte['put_cum']  = saved.get('0dte_put_cum',  0.0)
            _delta_flow_0dte['day']      = today
        print(f'[delta_flow] Restored all: '
              f'call={_delta_flow["call_cum"]/1e6:+.1f}M  '
              f'put={_delta_flow["put_cum"]/1e6:+.1f}M  |  '
              f'0dte call={_delta_flow_0dte["call_cum"]/1e6:+.1f}M  '
              f'0dte put={_delta_flow_0dte["put_cum"]/1e6:+.1f}M')
    except Exception as e:
        print(f'[ERROR] delta_flow load: {e}')


def get_series() -> dict:
    """Return all-expiry call/put series."""
    with _delta_flow_lock:
        return {
            'call': list(_delta_flow['call']),
            'put':  list(_delta_flow['put']),
        }


def get_series_0dte() -> dict:
    """Return 0DTE-only call/put series."""
    with _delta_flow_lock:
        return {
            'call': list(_delta_flow_0dte['call']),
            'put':  list(_delta_flow_0dte['put']),
        }


# ── Core accumulation ─────────────────────────────────────────────────────────────

def _dealer_delta_sign(side: str, direction: str) -> float:
    if side == 'call':
        return +1.0 if direction == 'BUY' else -1.0
    else:
        return -1.0 if direction == 'BUY' else +1.0


def _accumulate_into(store: dict, side: str, notional: float, time_s: int) -> float:
    """Add notional into a delta flow store dict. Returns new cumulative. Called inside lock."""
    now_et = datetime.now(tz=ET)
    today  = now_et.date()

    if store['day'] != today and (
            now_et.hour > 9 or (now_et.hour == 9 and now_et.minute >= 30)):
        store['call']     = []
        store['put']      = []
        store['call_cum'] = 0.0
        store['put_cum']  = 0.0
        store['day']      = today

    cum_key = f'{side}_cum'
    store[cum_key] += notional
    cum = store[cum_key]

    points = store[side]
    if points and points[-1]['time'] == time_s:
        points[-1]['value'] = cum
    else:
        points.append({'time': time_s, 'value': cum})

    return cum


def accumulate(side: str, notional: float, time_s: int, is_0dte: bool = False):
    """
    Add notional to _delta_flow (all-expiry) and optionally _delta_flow_0dte.
    Persist and push SSE. Shared by chain-diff and streaming alert paths.
    """
    with _delta_flow_lock:
        cum = _accumulate_into(_delta_flow, side, notional, time_s)
        if is_0dte:
            _accumulate_into(_delta_flow_0dte, side, notional, time_s)
        _save_delta_flow()

    if _sse_push:
        _sse_push({
            'type':       'delta_flow',
            'side':       side,
            'time':       time_s,
            'cumulative': cum,
            'delta':      notional,
            'is_0dte':    is_0dte,
        })


# ── Streaming alert path ──────────────────────────────────────────────────────────

def record_alert(alert: dict):
    """
    Accumulate signed delta notional from a single streaming flow alert.
    Called by on_flow_alert() for large prints detected by the streamer.
    """
    side        = alert['side']
    direction   = alert['direction']
    volume      = alert['volume_delta']
    strike      = float(alert['strike'])
    chain_delta = abs(float(alert.get('delta', 0.50))) or 0.50
    is_0dte     = bool(alert.get('is_0dte', False))
    notional    = _dealer_delta_sign(side, direction) * volume * strike * 100 * chain_delta
    time_s      = int(datetime.now(tz=ET).timestamp())
    accumulate(side, notional, time_s, is_0dte=is_0dte)


# ── Full-book chain diff path ─────────────────────────────────────────────────────

def extract_chain_snapshot(chain: dict) -> dict:
    """
    Flatten the full SPX option chain into a dict keyed by option symbol.
    Captures every strike across every expiry.
    Returns: { symbol -> { volume, bid, ask, last, delta, strike, side, is_0dte } }
    """
    snap = {}
    for side, exp_map in (('call', chain.get('callExpDateMap', {})),
                          ('put',  chain.get('putExpDateMap',  {}))):
        for exp_key, strikes in exp_map.items():
            is_0dte = exp_key.endswith(':0')
            for strike_str, opts in strikes.items():
                if not opts:
                    continue
                opt = opts[0]
                sym = opt.get('symbol')
                if not sym:
                    continue
                raw_delta = opt.get('delta') or (0.50 if side == 'call' else -0.50)
                snap[sym] = {
                    'volume':  int(opt.get('totalVolume') or 0),
                    'bid':     float(opt.get('bid')  or 0),
                    'ask':     float(opt.get('ask')  or 0),
                    'last':    float(opt.get('last') or 0),
                    'delta':   float(raw_delta),
                    'strike':  float(strike_str),
                    'side':    side,
                    'is_0dte': is_0dte,
                }
    return snap


def process_chain_snapshot(new_snap: dict):
    """
    Diff new chain snapshot against the previous one.
    For every contract with new volume, compute signed dealer delta notional
    and accumulate. Direction inferred from last vs bid/ask midpoint.
    Only runs during market hours (09:00–17:00 ET).
    """
    now_et = datetime.now(tz=ET)
    if not (9 <= now_et.hour < 17):
        return

    with _spx_chain_lock:
        prev = dict(_spx_chain_prev)
        _spx_chain_prev.clear()
        _spx_chain_prev.update(new_snap)

    if not prev:
        return  # first run — establish baseline, don't accumulate yet

    total_call      = 0.0
    total_put       = 0.0
    total_call_0dte = 0.0
    total_put_0dte  = 0.0
    contracts_hit   = 0

    for sym, cur in new_snap.items():
        p = prev.get(sym)
        if p is None:
            continue
        vol_delta = cur['volume'] - p['volume']
        if vol_delta <= 0:
            continue

        mid       = (cur['bid'] + cur['ask']) / 2 if cur['ask'] > 0 else 0
        direction = 'BUY' if (mid == 0 or cur['last'] >= mid) else 'SELL'
        sign      = _dealer_delta_sign(cur['side'], direction)
        abs_d     = abs(cur['delta']) or 0.50
        notional  = sign * vol_delta * cur['strike'] * 100 * abs_d

        if cur['side'] == 'call':
            total_call += notional
            if cur['is_0dte']:
                total_call_0dte += notional
        else:
            total_put += notional
            if cur['is_0dte']:
                total_put_0dte += notional
        contracts_hit += 1

    if contracts_hit == 0:
        return

    time_s = int(now_et.timestamp())
    accumulate('call', total_call, time_s, is_0dte=False)
    accumulate('put',  total_put,  time_s, is_0dte=False)

    # Accumulate 0DTE bucket separately (only updates _delta_flow_0dte)
    with _delta_flow_lock:
        _accumulate_into(_delta_flow_0dte, 'call', total_call_0dte, time_s)
        _accumulate_into(_delta_flow_0dte, 'put',  total_put_0dte,  time_s)
        _save_delta_flow()

    net      = (total_call + total_put) / 1e6
    net_0dte = (total_call_0dte + total_put_0dte) / 1e6
    print(
        f'[{now_et.strftime("%H:%M:%S")}] Chain flow: {contracts_hit} contracts'
        f'  call={total_call/1e6:+.1f}M  put={total_put/1e6:+.1f}M  net={net:+.1f}M'
        f'  | 0DTE net={net_0dte:+.1f}M'
    )
