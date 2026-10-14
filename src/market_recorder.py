"""
market_recorder.py — saves the raw market data a model can learn from: where every
option's price was, where it went, and what ES and GEX were doing around it.

One file per day in data/ (finished days are gzipped by the daily job, kept forever):

  option_ticks_SPX_<date>.csv  every streamed option quote (0DTE book + the next
                               expiry's top strikes): bid, ask, last, day volume
  option_bars_SPX_<date>.csv   per streamed contract per minute: mid open/high/low/
                               close, bid/ask/last at the close, day volume, quote
                               count, live IV / delta / gamma, open interest, SPX
  option_chain_SPX_<date>.csv  the expirations the stream doesn't cover, from the
                               60 s chain refresh: the coming week and the monthly
                               every refresh, the rest every 5 min; +-200 pts
  es_ticks_<date>.csv          every /ES level-one update: bid, ask, sizes, last,
                               last size, day volume
  es_book_<date>.csv           /ES per minute: book at the close, spread avg/max,
                               size imbalance, quote / trade counts, traded volume,
                               VWAP, large trades, last-price high/low/close

Options are recorded 9:30-16:00 ET on weekdays, ES during Globex hours. Rows are
buffered and written once a second by flush_loop(); minute bars are closed there too.
"""
import csv
import io
import os
import threading
import time
from datetime import date, datetime, timedelta

from zoneinfo import ZoneInfo

_SRC_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(os.path.dirname(_SRC_DIR), 'data')
ET        = ZoneInfo('America/New_York')

CHAIN_BAND_PTS   = 200      # strikes within this of spot
CHAIN_NEAR_DAYS  = 7        # expirations this close are saved every refresh
CHAIN_FAR_EVERY  = 300      # seconds between snapshots of the other expirations
ES_LARGE_LOTS    = 20       # a trade this size or bigger counts as large

OPTION_TICK_FIELDS = ['ts_ms', 'symbol', 'bid', 'ask', 'last', 'volume']
OPTION_BAR_FIELDS  = ['time', 'symbol', 'expiry', 'strike', 'side', 'mid_open', 'mid_high',
                      'mid_low', 'mid_close', 'bid', 'ask', 'last', 'volume', 'quotes',
                      'iv', 'delta', 'gamma', 'oi', 'spot']
CHAIN_FIELDS       = ['time', 'expiry', 'dte', 'strike', 'side', 'root', 'bid', 'ask', 'last',
                      'mark', 'volume', 'oi', 'iv', 'delta', 'gamma', 'theta', 'vega', 'spot']
ES_TICK_FIELDS     = ['ts_ms', 'bid', 'ask', 'bid_size', 'ask_size', 'last', 'last_size', 'volume']
ES_BOOK_FIELDS     = ['time', 'bid', 'ask', 'bid_size', 'ask_size', 'spread_avg', 'spread_max',
                      'imbalance_avg', 'quotes', 'trades', 'volume', 'vwap', 'large_trades',
                      'large_volume', 'high', 'low', 'close']

# Providers the dashboard plugs in (background.py): contract meta / greeks / spot
_meta_fn   = lambda symbol: None          # -> (expiry 'YYYY-MM-DD', strike, 'call'|'put')
_greeks_fn = lambda symbol: (None, None, None)   # -> (iv, delta, gamma)
_oi_fn     = lambda symbol: None
_spot_fn   = lambda: None


def configure(meta_fn=None, greeks_fn=None, oi_fn=None, spot_fn=None):
    global _meta_fn, _greeks_fn, _oi_fn, _spot_fn
    _meta_fn   = meta_fn or _meta_fn
    _greeks_fn = greeks_fn or _greeks_fn
    _oi_fn     = oi_fn or _oi_fn
    _spot_fn   = spot_fn or _spot_fn


# ── trading hours / calendar ──────────────────────────────────────────────────

def _et(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, ET)


def options_open(ts: float) -> bool:
    t = _et(ts)
    return t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (16, 0)


def es_open(ts: float) -> bool:
    """CME Globex: Sun 18:00 - Fri 17:00 ET, halted 17:00-18:00 daily."""
    t = _et(ts)
    wd, hm = t.weekday(), (t.hour, t.minute)
    if wd == 5 or (wd == 6 and hm < (18, 0)) or (wd == 4 and hm >= (17, 0)):
        return False
    return not ((17, 0) <= hm < (18, 0))


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)                      # the 3rd Friday falls on the 15th-21st
    return d + timedelta(days=(4 - d.weekday()) % 7)


def next_monthly(today: date) -> date:
    """The standard monthly expiration (3rd Friday) on or after today."""
    m = third_friday(today.year, today.month)
    if m < today:
        y, mo = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        m = third_friday(y, mo)
    return m


# ── buffered day files ────────────────────────────────────────────────────────

class _DayCsv:
    """Rows buffered in memory, appended to data/<prefix>_<date>.csv on flush()."""

    def __init__(self, prefix: str, fields: list):
        self.prefix, self.fields = prefix, fields
        self._rows = {}                            # day -> [row, ...]
        self._lock = threading.Lock()

    def add(self, day: str, row: list):
        with self._lock:
            self._rows.setdefault(day, []).append(row)

    def flush(self, data_dir: str | None = None):
        with self._lock:
            pending, self._rows = self._rows, {}
        for day, rows in pending.items():
            path = os.path.join(data_dir or DATA_DIR, f'{self.prefix}_{day}.csv')
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator='\n')
            if not os.path.exists(path):
                w.writerow(self.fields)
            w.writerows(rows)
            try:
                with open(path, 'a', encoding='utf-8', newline='') as f:
                    f.write(buf.getvalue())
            except OSError as e:
                print(f'[recorder] {self.prefix}: write failed: {e}')


option_ticks = _DayCsv('option_ticks_SPX', OPTION_TICK_FIELDS)
option_bars  = _DayCsv('option_bars_SPX', OPTION_BAR_FIELDS)
option_chain = _DayCsv('option_chain_SPX', CHAIN_FIELDS)
es_ticks     = _DayCsv('es_ticks', ES_TICK_FIELDS)
es_book      = _DayCsv('es_book', ES_BOOK_FIELDS)
ALL_FILES    = (option_ticks, option_bars, option_chain, es_ticks, es_book)


def _r(v, nd):
    return '' if v is None else round(v, nd)


def _day(ts: float) -> str:
    return _et(ts).date().isoformat()


# ── options: ticks + minute bars ──────────────────────────────────────────────

_opt_lock  = threading.Lock()
_opt_bar   = {}     # symbol -> bar being built: minute, o/h/l/c (mid), n, close bid/ask/last/volume
_opt_prev  = {}     # symbol -> the bar before it (a new minute's quote can beat the flusher)
_opt_minute = None  # minute (epoch s) the flusher closes next


def option_quote(symbol: str, bid, ask, last, volume, ts: float | None = None):
    """One streamed option quote (bid/ask/last/volume already filled from memory)."""
    ts = ts or time.time()
    if not options_open(ts):
        return
    option_ticks.add(_day(ts), [int(ts * 1000), symbol, bid, ask, last, volume])
    mid = (bid + ask) / 2 if bid and ask and bid > 0 and ask > 0 else None
    minute = int(ts // 60) * 60
    with _opt_lock:
        b = _opt_bar.get(symbol)
        if b is None or b['minute'] != minute:
            if b is not None:
                _opt_prev[symbol] = b
            carry = b['c'] if b else None            # the minute opens where the last one closed
            b = _opt_bar[symbol] = {'minute': minute, 'o': None, 'h': None, 'l': None, 'c': carry, 'n': 0}
        b['n'] += 1
        b.update(bid=bid, ask=ask, last=last, volume=volume)
        if mid is not None:
            if b['o'] is None:
                b['o'] = b['h'] = b['l'] = mid
            b['h'], b['l'], b['c'] = max(b['h'], mid), min(b['l'], mid), mid


def close_option_minute(minute: int):
    """Write one bar per known contract for `minute` (epoch s). A contract with no
    quote that minute repeats its last close with quotes=0, so every contract has a
    row every minute (until it first quotes, it has none)."""
    if not options_open(minute):
        return
    spot = _spot_fn()
    day = _day(minute)
    with _opt_lock:
        items = [(s, dict(b), dict(_opt_prev.get(s) or {})) for s, b in _opt_bar.items()]
    for symbol, cur, prev in items:
        meta = _meta_fn(symbol)
        if not meta:
            continue
        if cur['minute'] == minute:
            bar = cur
        elif prev.get('minute') == minute:
            bar = prev
        else:                                        # no quote this minute: carry the last close
            src = cur if cur['minute'] < minute else (prev if prev.get('minute', minute) < minute else None)
            if not src:
                continue
            bar = {**src, 'o': src.get('c'), 'h': src.get('c'), 'l': src.get('c'), 'n': 0}
        o = bar['o'] if bar['o'] is not None else bar.get('c')
        h = bar['h'] if bar['h'] is not None else bar.get('c')
        l = bar['l'] if bar['l'] is not None else bar.get('c')
        expiry, strike, side = meta
        iv, dl, gm = _greeks_fn(symbol)
        option_bars.add(day, [minute, symbol, expiry, strike, side, _r(o, 3), _r(h, 3), _r(l, 3),
                              _r(bar.get('c'), 3), bar.get('bid'), bar.get('ask'), bar.get('last'),
                              bar.get('volume'), bar['n'], _r(iv, 5), _r(dl, 4), _r(gm, 7),
                              _oi_fn(symbol), _r(spot, 2)])


# ── option chain (expirations the stream doesn't cover) ───────────────────────

_chain_far_at = 0.0


def record_chain(chain: dict, spot: float, streamed: set, ts: float | None = None):
    """Save the chain's non-0DTE contracts within CHAIN_BAND_PTS of spot that aren't
    streamed: expirations within CHAIN_NEAR_DAYS and the monthly on every call, the
    rest every CHAIN_FAR_EVERY seconds. Returns the number of rows."""
    global _chain_far_at
    ts = ts or time.time()
    if not options_open(ts) or not spot:
        return 0
    today = _et(ts).date()
    monthly = next_monthly(today)
    far_due = ts - _chain_far_at >= CHAIN_FAR_EVERY - 5
    day, n = _day(ts), 0
    for side, key in (('call', 'callExpDateMap'), ('put', 'putExpDateMap')):
        for exp_key, strikes in (chain.get(key) or {}).items():
            exp = date.fromisoformat(exp_key.split(':')[0])
            if exp <= today:
                continue                                   # 0DTE: streamed
            if (exp - today).days > CHAIN_NEAR_DAYS and exp != monthly and not far_due:
                continue
            for strike_s, opts in strikes.items():
                k = float(strike_s)
                if abs(k - spot) > CHAIN_BAND_PTS:
                    continue
                for o in opts or []:
                    if o.get('symbol') in streamed:
                        continue
                    option_chain.add(day, [int(ts), exp.isoformat(), (exp - today).days, k, side,
                                           (o.get('symbol') or '').split(' ')[0], o.get('bid'), o.get('ask'),
                                           o.get('last'), o.get('mark'), o.get('totalVolume'),
                                           o.get('openInterest'), o.get('volatility'), o.get('delta'),
                                           o.get('gamma'), o.get('theta'), o.get('vega'), round(spot, 2)])
                    n += 1
    if far_due:
        _chain_far_at = ts
    return n


# ── ES: ticks + minute book summary ───────────────────────────────────────────

_es_lock  = threading.Lock()
_es_state = {}      # last known: bid, ask, bid_size, ask_size, last, last_size, volume
_es_min   = None    # current minute aggregate


def _book_now():
    """(spread, size imbalance) of the current book, either None if unknown."""
    st = _es_state
    bid, ask, bs_, as_ = st.get('bid'), st.get('ask'), st.get('bid_size'), st.get('ask_size')
    spread = ask - bid if bid and ask and ask >= bid else None
    imb = (bs_ - as_) / (bs_ + as_) if bs_ is not None and as_ is not None and bs_ + as_ > 0 else None
    return spread, imb


def _es_new_minute(minute):
    spread, _ = _book_now()                    # the book carried in from the last minute
    return {'minute': minute, 'last_ts': float(minute), 'spread_tw': 0.0, 'spread_t': 0.0,
            'spread_max': spread, 'imb_tw': 0.0, 'imb_t': 0.0, 'quotes': 0, 'trades': 0,
            'volume': 0, 'pv': 0.0, 'large_trades': 0, 'large_volume': 0, 'high': None, 'low': None}


def _accrue(m, until: float):
    """Time-weight the book as it stood since the last update (caller holds _es_lock):
    a book that sat lopsided for 50 s outweighs one that flickered for 1 s."""
    dt = until - m['last_ts']
    if dt > 0:
        spread, imb = _book_now()
        if spread is not None:
            m['spread_tw'] += spread * dt
            m['spread_t'] += dt
        if imb is not None:
            m['imb_tw'] += imb * dt
            m['imb_t'] += dt
    m['last_ts'] = max(m['last_ts'], until)


def es_quote(fields: dict, ts: float | None = None):
    """One /ES level-one update: any of bid, ask, bid_size, ask_size, last,
    last_size, volume (only the fields that changed)."""
    global _es_min
    ts = ts or time.time()
    if not es_open(ts):
        return
    minute = int(ts // 60) * 60
    with _es_lock:
        if _es_min is None or _es_min['minute'] != minute:
            _close_es(_es_min)                     # with the book as it stood at the close
            _es_min = _es_new_minute(minute)
        m = _es_min
        _accrue(m, ts)                             # the book before this update, up to now
        prev_vol = _es_state.get('volume')
        _es_state.update({k: v for k, v in fields.items() if v is not None})
        st = dict(_es_state)
        m['quotes'] += 1
        spread, _ = _book_now()
        if spread is not None:
            m['spread_max'] = spread if m['spread_max'] is None else max(m['spread_max'], spread)
        vol = fields.get('volume')
        # day volume restarts after the 17:00-18:00 halt: the new total is all since then
        dv = 0 if vol is None or prev_vol is None else (vol - prev_vol if vol >= prev_vol else vol)
        if dv > 0 and st.get('last'):
            m['trades'] += 1
            m['volume'] += dv
            m['pv'] += st['last'] * dv
            if (st.get('last_size') or 0) >= ES_LARGE_LOTS:
                m['large_trades'] += 1
                m['large_volume'] += dv
        # a trade at an unchanged price comes as volume only: it still sets high / low
        # (otherwise VWAP could fall outside them)
        last = fields.get('last') or (st.get('last') if dv > 0 else None)
        if last:
            m['high'] = last if m['high'] is None else max(m['high'], last)
            m['low'] = last if m['low'] is None else min(m['low'], last)
    es_ticks.add(_day(ts), [int(ts * 1000), st.get('bid'), st.get('ask'), st.get('bid_size'),
                            st.get('ask_size'), st.get('last'), st.get('last_size'), st.get('volume')])


def _close_es(m):
    """Write a finished ES minute. Caller holds _es_lock."""
    if not m:
        return
    _accrue(m, m['minute'] + 60)               # the book held to the end of the minute
    st = _es_state
    q = m['quotes']
    es_book.add(_day(m['minute']), [
        m['minute'], st.get('bid'), st.get('ask'), st.get('bid_size'), st.get('ask_size'),
        _r(m['spread_tw'] / m['spread_t'], 4) if m['spread_t'] else '',
        m['spread_max'] if m['spread_max'] is not None else '',
        _r(m['imb_tw'] / m['imb_t'], 4) if m['imb_t'] else '', q, m['trades'], m['volume'],
        _r(m['pv'] / m['volume'], 3) if m['volume'] else '', m['large_trades'], m['large_volume'],
        m['high'] if m['high'] is not None else '', m['low'] if m['low'] is not None else '',
        st.get('last')])


# ── volatility indexes / internals: minute bars ───────────────────────────────

INTERNAL_FIELDS = ['time', 'symbol', 'open', 'high', 'low', 'close', 'volume', 'updates']
internals = _DayCsv('internals', INTERNAL_FIELDS)
ALL_FILES = ALL_FILES + (internals,)

_int_lock = threading.Lock()
_int_bar  = {}      # symbol -> {'minute', 'o', 'h', 'l', 'c', 'volume', 'n'}
_int_minute = None


def internal_quote(symbol: str, last, volume, ts: float | None = None):
    """A VIX / TICK / TRIN / SPY ... update (fields that changed; None = unchanged)."""
    ts = ts or time.time()
    if not options_open(ts):
        return
    minute = int(ts // 60) * 60
    with _int_lock:
        b = _int_bar.get(symbol)
        if b is None or b['minute'] < minute:
            if b is not None and b['minute'] < minute and b.get('c') is not None:
                _write_internal(symbol, b)               # minute ended before the flusher saw it
            c = b['c'] if b else None
            b = _int_bar[symbol] = {'minute': minute, 'o': None, 'h': None, 'l': None, 'c': c,
                                    'volume': b['volume'] if b else None, 'n': 0, 'written': False}
        b['n'] += 1
        if volume is not None:
            b['volume'] = volume
        if last is not None:
            if b['o'] is None:
                b['o'] = b['h'] = b['l'] = last
            b['h'], b['l'], b['c'] = max(b['h'], last), min(b['l'], last), last


def _write_internal(symbol, b):
    """Caller holds _int_lock. A minute with no update repeats the last close."""
    if b.get('written') or b.get('c') is None:
        return
    o = b['o'] if b['o'] is not None else b['c']
    h = b['h'] if b['h'] is not None else b['c']
    l = b['l'] if b['l'] is not None else b['c']
    internals.add(_day(b['minute']), [b['minute'], symbol, o, h, l, b['c'], b.get('volume'), b['n']])
    b['written'] = True


def close_internal_minute(minute: int):
    """Write every symbol's bar for `minute`; symbols quiet that minute carry their close."""
    if not options_open(minute):
        return
    with _int_lock:
        for symbol, b in list(_int_bar.items()):
            if b['minute'] == minute:
                _write_internal(symbol, b)
            elif b['minute'] < minute and b.get('c') is not None:
                _write_internal(symbol, b)                # its own minute, if not yet written
                nb = _int_bar[symbol] = {'minute': minute, 'o': None, 'h': None, 'l': None,
                                         'c': b['c'], 'volume': b.get('volume'), 'n': 0, 'written': False}
                _write_internal(symbol, nb)


# ── background flusher ────────────────────────────────────────────────────────

def tick(now: float | None = None):
    """Close finished minutes and write buffered rows. flush_loop() calls this every second."""
    global _opt_minute, _int_minute, _es_min
    now = now or time.time()
    minute = int(now // 60) * 60
    if _opt_minute is None:
        _opt_minute = minute
    elif minute > _opt_minute:
        close_option_minute(_opt_minute)
        _opt_minute = minute
    if _int_minute is None:
        _int_minute = minute
    elif minute > _int_minute:
        close_internal_minute(_int_minute)
        _int_minute = minute
    with _es_lock:                   # ES minute with no update since it ended
        if _es_min is not None and _es_min['minute'] < minute:
            _close_es(_es_min)
            _es_min = None
    for f in ALL_FILES:
        f.flush()


def flush_loop():
    while True:
        time.sleep(1.0)
        try:
            tick()
        except Exception as e:       # never kill the thread
            print(f'[recorder] flush failed: {e}')
