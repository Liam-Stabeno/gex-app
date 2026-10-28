"""
delta_flow.py — Cumulative dealer delta (Δ FLOW) from every streamed option trade.

Each trade (a rise in a contract's day volume between two of its ticks) is classed
against the quote standing before it (Lee-Ready): at or through the ask = bought, at or
through the bid = sold, inside = whichever side of the mid it printed, at the mid = the
tick test (up from the last trade = bought). Prints more than a spread outside the quote
are skipped: they are usually one leg of a multi-leg order, priced as a package.

    notional = sign × contracts × delta × 100 × SPX   (put delta < 0)

That is the delta customers bought, which dealers must buy to stay hedged:
  call bought → +   call sold → -   put bought → -   put sold → +

Two series: the streamed set (today's 0DTE book + the next expiration's top strikes) and
0DTE only. Tested on 10 days of recorded ticks (tools/tick_flow_test.py): it moves with
SPX (0DTE same-minute corr +0.63, 15-30 min +0.4-0.7) but does not lead it.

Sessions run 9:30-16:15 ET. One point per minute (the cumulative at that minute), saved
to data/delta_flow_ticks_YYYY-MM-DD.json; the older chain-diff files (delta_flow_*.json)
are left as they were.
"""

import json
import os
import threading
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

_SRC_DIR  = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(os.path.dirname(_SRC_DIR), 'data')

SESSION     = (dtime(9, 30), dtime(16, 15))
SAVE_EVERY  = 10.0     # s between saves (and at each new minute)
PUSH_EVERY  = 1.0      # s between SSE pushes
SERIES      = ('call', 'put', '0dte_call', '0dte_put')

_lock  = threading.Lock()
_state = {'day': None, 'cum': dict.fromkeys(SERIES, 0.0), 'points': {k: [] for k in SERIES}}
_last  = {}            # symbol -> (bid, ask, last, volume): the tick before
_saved_at  = 0.0
_pushed_at = 0.0
_sse_push  = None


def set_sse_push(fn):
    """Inject the SSE push function. Called from dashboard.py at startup."""
    global _sse_push
    _sse_push = fn


def _path(day: str, data_dir: str | None = None) -> str:
    return os.path.join(data_dir or _DATA_DIR, f'delta_flow_ticks_{day}.json')


# ── classification ───────────────────────────────────────────────────────────────

def classify(bid, ask, prev_last, last) -> int | None:
    """+1 bought, -1 sold, 0 unknown, against the quote standing before the trade; None
    for a print more than a spread outside it (a multi-leg order's leg) or no quote."""
    if not last or last <= 0 or bid is None or ask is None or not (ask > 0 and 0 <= bid <= ask):
        return None
    spread = ask - bid
    if last > ask + spread or last < bid - spread:
        return None
    if last >= ask:
        return 1
    if last <= bid:
        return -1
    mid = (bid + ask) / 2
    if last != mid:
        return 1 if last > mid else -1
    if prev_last is None or last == prev_last:
        return 0
    return 1 if last > prev_last else -1


# ── accumulation ─────────────────────────────────────────────────────────────────

def _in_session(dt: datetime) -> bool:
    return dt.weekday() < 5 and SESSION[0] <= dt.time() < SESSION[1]


def _roll(day: str):
    """Start `day` (inside _lock)."""
    _state['day'] = day
    _state['cum'] = dict.fromkeys(SERIES, 0.0)
    _state['points'] = {k: [] for k in SERIES}


def record_tick(symbol: str, bid, ask, last, volume, contract, ts: float | None = None,
                data_dir: str | None = None):
    """One streamed option tick (bid/ask/last/volume filled from memory). `contract()` ->
    (side 'call'|'put', is_0dte, delta, spot) or None; only called when the tick is a
    trade."""
    global _saved_at, _pushed_at
    ts = ts or time.time()
    prev = _last.get(symbol)
    _last[symbol] = (bid, ask, last, volume)
    if prev is None or volume is None or prev[3] is None or volume <= prev[3]:
        return                     # quote-only tick, the first one seen, or the day's reset
    dt = datetime.fromtimestamp(ts, ET)
    if not _in_session(dt):
        return
    sign = classify(prev[0], prev[1], prev[2], last)
    if not sign:
        return
    info = contract()
    if not info:
        return
    side, is_0dte, dl, spot = info
    if not dl or not spot or side not in ('call', 'put'):
        return
    notional = sign * (volume - prev[3]) * dl * 100 * spot
    minute = int(ts // 60) * 60
    with _lock:
        day = dt.date().isoformat()
        if _state['day'] != day:
            _roll(day)
        new_minute = False
        for key in (side, '0dte_' + side) if is_0dte else (side,):
            _state['cum'][key] += notional
            pts = _state['points'][key]
            if pts and pts[-1]['time'] == minute:
                pts[-1]['value'] = _state['cum'][key]
            else:
                pts.append({'time': minute, 'value': _state['cum'][key]})
                new_minute = True
        if new_minute or ts - _saved_at >= SAVE_EVERY:
            _save(data_dir)
            _saved_at = ts
        push = _sse_push and ts - _pushed_at >= PUSH_EVERY
        if push:
            _pushed_at = ts
            msg = {'type': 'delta_flow', 'time': minute, **{k: _state['cum'][k] for k in SERIES}}
    if push:
        _sse_push(msg)


# ── persistence ──────────────────────────────────────────────────────────────────

def _save(data_dir: str | None = None):
    """Write today's series (inside _lock)."""
    if not _state['day']:
        return
    path = _path(_state['day'], data_dir)
    try:
        with open(path + '.tmp', 'w') as f:
            json.dump({'day': _state['day'], 'cum': _state['cum'], 'points': _state['points']}, f)
        os.replace(path + '.tmp', path)
    except Exception as e:
        print(f'[ERROR] delta_flow save: {e}')


def load_today(now: datetime | None = None, data_dir: str | None = None):
    """Today's saved series, after a restart."""
    day = (now or datetime.now(ET)).date().isoformat()
    path = _path(day, data_dir)
    if not os.path.exists(path):
        print('[delta_flow] No saved session for today — starting fresh')
        return
    try:
        with open(path) as f:
            saved = json.load(f)
        with _lock:
            _roll(day)
            _state['cum'].update({k: float(v) for k, v in saved.get('cum', {}).items() if k in SERIES})
            _state['points'].update({k: v for k, v in saved.get('points', {}).items() if k in SERIES})
        c = _state['cum']
        print(f'[delta_flow] Restored: call={c["call"] / 1e6:+.1f}M put={c["put"] / 1e6:+.1f}M | '
              f'0dte call={c["0dte_call"] / 1e6:+.1f}M put={c["0dte_put"] / 1e6:+.1f}M')
    except Exception as e:
        print(f'[ERROR] delta_flow load: {e}')


def _series(prefix: str, now: datetime | None = None) -> dict:
    today = (now or datetime.now(ET)).date().isoformat()
    with _lock:
        if _state['day'] != today:
            return {'call': [], 'put': []}
        return {'call': list(_state['points'][prefix + 'call']), 'put': list(_state['points'][prefix + 'put'])}


def get_series(now: datetime | None = None) -> dict:
    """Today's streamed-set call/put series: [{time (minute, unix s), value (cumulative $)}]."""
    return _series('', now)


def get_series_0dte(now: datetime | None = None) -> dict:
    """Today's 0DTE-only call/put series."""
    return _series('0dte_', now)
