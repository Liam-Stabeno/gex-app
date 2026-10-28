"""
Prediction scoreboard: does GEX call SPX's next move?

Every 15 minutes of the session the app writes down a few specific predictions from the
GEX in force at that moment, then grades each one once its time is up, against SPX
1-minute bars. It runs back over every recorded day and on today live, so the page shows
a hit rate for each prediction next to what a guess without GEX would score.

SIZE · REGIME    net GEX (all expirations) negative -> the next 15 / 30 / 60 min range is
                 bigger than usual; positive -> smaller. Usual = the median range of that
                 same clock window over the previous BASE_SESSIONS sessions (price only, so
                 it was known at the time).
SIZE · AT PRICE  the same call from GEX at the price (the brakes reading) instead.
                 Both are compared with the no-GEX guess: the last 15 min moved more than
                 usual -> the next will too (volatility clusters, with or without gamma).
LEVEL            price reaches a gamma level (support, resistance, trapdoor, squeeze, the 3
                 walls, call / put wall): a positive-GEX strike holds, a negative one breaks.
                 break = a 1-min close BREAK_PTS through it; hold = a close HOLD_PTS back on
                 the side price came from; whichever comes first.
RUN              after a negative-GEX level breaks, price runs RUN_PTS further (from the
                 breaking close) before closing back through the level. Compared with the
                 same after positive-GEX levels break.
PIN              14:00, 14:30, 15:00, 15:30 with 0DTE gamma positive: the close lands nearer
                 the pin (True Pin, 0DTE pin) than price is then. Compared with the same
                 when 0DTE gamma is negative.
TOUCH TRADE      price reaches a level (gold strike, 0DTE put wall, put wall, pin, trapdoor,
                 the previous close): the 0DTE option nearest the money, bought at the
                 stream's ask, sold at its bid 15 / 30 / 60 min later. bounce = a call if
                 price came down to the level, a put if it came up; break = the other. Each
                 level is graded on the trade tools/odte_level_test.py found promising
                 (2026-10-28, 12 days), against the same trade every 15 min at no level.

No prediction looks ahead: each uses the latest snapshot at or before its time, and the
bars from its time on. Graded predictions of finished days are appended to
data/predictions_SPX.jsonl (never rewritten: a rules change saves the days again under a
new RULES_VERSION). Everything is recomputable from the recorded day files.
"""

import csv
import glob
import json
import os
import statistics
import threading
import time
from bisect import bisect_right
from datetime import date, datetime, time as dtime

import gex_stats
from gex_stats import ET, day_file, open_text
from price_history import load_candles

DATA_DIR = gex_stats._DATA_DIR
SAVE_FILE = os.path.join(DATA_DIR, 'predictions_SPX.jsonl')
RULES_VERSION = 2                # 2: touch trades

HORIZONS = (15, 30, 60)          # minutes
FIRST_CHECK_MIN = 15             # 9:45: the first 15 min trade the open's orders, not gamma
CHECK_EVERY_MIN = 15
SESSION_MIN = 390                # 9:30-16:00
BASE_SESSIONS = 20
STALE_SEC = 900                  # a snapshot older than this isn't "in force"

TOUCH_PTS = 1.0                  # price within this of a level = it got there
ARM_PTS = 5.0                    # ...after being at least this far away (one event per approach)
BREAK_PTS = 3.0
HOLD_PTS = 5.0
RUN_PTS = 10.0
LABEL_MEMORY_SEC = 900           # a strike named in the last 15 min still counts as a level:
                                 # support / resist step aside within 7.5 pts of price
PIN_TIMES = (270, 300, 330, 360)  # minutes after 9:30: 14:00, 14:30, 15:00, 15:30
PIN_MIN_DIST = 3.0               # price already this close to the pin: nothing to predict

NAMED = ('support', 'resistance', 'trapdoor', 'squeeze')

# touch trades (the rules tools/odte_level_test.py used)
TRADE_TOUCH_PTS = 2.0            # within this of the level...
TRADE_APPROACH_PTS = 5.0         # ...after a close at least this far away in the last
TRADE_LOOKBACK_MIN = 10          # this many minutes
TRADE_COOLDOWN_MIN = 15          # one touch per level per this long
TRADE_FIRST_MIN, TRADE_LAST_MIN = 5, 360    # entries 9:35-15:30
TRADE_HIT = 1.25                 # 'hit +25%': the bid reached this x the ask paid within 60 min
TRADE_LEVELS = {                 # level -> the trade it's graded on
    'gold': 'bounce', '0DTE put wall': 'bounce', 'put wall': 'bounce', 'pin': 'bounce',
    'trapdoor': 'break', 'prev close': 'bounce'}
MIDDAY = (150, 270)              # 12:00-14:00: bounces at any of them, graded together


# ── inputs ─────────────────────────────────────────────────────────────────────

_candles = {'sig': None, 'at': 0.0, 'sessions': {}}
_candles_lock = threading.Lock()


def sessions(sym: str = 'SPX', max_age: float = 60.0) -> dict:
    """{date iso: [(epoch s, open, high, low, close)] for 9:30-15:59 ET}, from the 1-min
    price history. Re-read when the file changes, at most every `max_age` s."""
    from price_history import csv_path
    path = csv_path(sym)
    try:
        st = os.stat(path)
    except OSError:
        return {}
    sig = (st.st_size, st.st_mtime)
    with _candles_lock:
        if _candles['sig'] == sig or time.time() - _candles['at'] < max_age and _candles['sig']:
            return _candles['sessions']
    out = {}
    for c in load_candles(sym):
        t = int(c['datetime']) // 1000
        dt = datetime.fromtimestamp(t, ET)
        if dt.weekday() < 5 and dtime(9, 30) <= dt.time() < dtime(16, 0):
            out.setdefault(dt.date().isoformat(), []).append(
                (t, float(c['open']), float(c['high']), float(c['low']), float(c['close'])))
    out = {d: sorted(v) for d, v in out.items()}
    with _candles_lock:
        _candles.update(sig=sig, at=time.time(), sessions=out)
    return out


def _open_ts(day: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), dtime(9, 30), ET).timestamp())


def _window(bars: list, t0: int, minutes: int) -> list:
    times = [b[0] for b in bars]
    return bars[bisect_right(times, t0 - 1):bisect_right(times, t0 + 60 * minutes - 1)]


def _range(bars: list) -> float:
    return max(b[2] for b in bars) - min(b[3] for b in bars)


def _check_times(horizon: int):
    return range(FIRST_CHECK_MIN, SESSION_MIN - horizon + 1, CHECK_EVERY_MIN)


def _by_minute(day: str, bars: list) -> list:
    """[(high, low) or None] for each minute after 9:30."""
    out, t_open = [None] * SESSION_MIN, _open_ts(day)
    for t, _, hi, lo, _ in bars:
        m = (t - t_open) // 60
        if 0 <= m < SESSION_MIN:
            out[m] = (hi, lo)
    return out


def baselines(day: str, by_day: dict) -> dict:
    """{(minute after 9:30, horizon): median range of that window} over the previous
    BASE_SESSIONS sessions with the window complete."""
    prior = [_by_minute(d, by_day[d]) for d in sorted(d for d in by_day if d < day)[-BASE_SESSIONS:]]
    out = {}
    for h in HORIZONS:
        for m in range(0, SESSION_MIN - h + 1, CHECK_EVERY_MIN):   # from 9:30: the no-GEX guess
                                                                    # looks at the 15 min before
            vals = []
            for mins in prior:
                w = [x for x in mins[m:m + h] if x]
                if len(w) >= 0.9 * h:
                    vals.append(max(x[0] for x in w) - min(x[1] for x in w))
            if len(vals) >= 5:
                out[(m, h)] = statistics.median(vals)
    return out


def snapshots(day: str, zero_dte: bool = False, data_dir: str = DATA_DIR) -> list:
    """[(epoch s, row)] from gex_snapshots (or gex_0dte_snapshots) for the day, in order.
    Their timestamps are this machine's local time."""
    path = day_file(f'gex_{"0dte_" if zero_dte else ""}snapshots_SPX_{day}.csv', data_dir)
    if not path:
        return []
    rows = []
    with open_text(path) as f:
        for r in csv.DictReader(f):
            try:
                rows.append((gex_stats._to_et(r['timestamp']).timestamp(), r))
            except (KeyError, ValueError, TypeError):
                continue
    rows.sort(key=lambda x: x[0])
    return rows


def _at(rows: list, t: float, field: str):
    """`field` of the latest row at or before t (None if older than STALE_SEC or blank)."""
    i = bisect_right([r[0] for r in rows], t) - 1
    if i < 0 or t - rows[i][0] > STALE_SEC:
        return None
    try:
        v = float(rows[i][1].get(field) or '')
    except ValueError:
        return None
    return v if v == v else None


def level_timeline(heat: dict) -> list:
    """[(epoch, {strike: [names]}, {strike: gex}, gex at price)] per heatmap snapshot,
    with the levels each one names (sticky to the one before, as the chart draws them)."""
    out, prev = [], None
    for t, col, sp in zip(heat.get('times', []), heat.get('all', []), heat.get('spots', [])):
        if not sp:
            continue
        L = gex_stats.gamma_levels(heat['strikes'], col, float(sp), prev=prev)
        if not L:
            continue
        prev = {k: (L[k]['strike'] if L.get(k) else None) for k in NAMED}
        names = {}
        for k in NAMED:
            if L.get(k):
                names.setdefault(L[k]['strike'], []).append(k)
        for w in L['walls']:
            names.setdefault(w['strike'], []).append('wall')
        out.append((int(t), names, dict(zip(heat['strikes'], col)), L['brakes']['gex']))
    return out


# ── predictions for one day ────────────────────────────────────────────────────

def predict_size(day: str, bars: list, base: dict, snaps: list, timeline: list, now: float) -> list:
    rows, t_open = [], _open_ts(day)
    ttimes = [x[0] for x in timeline]
    for m in _check_times(min(HORIZONS)):
        t0 = t_open + 60 * m
        if t0 > now:
            break
        h_out = {}
        for h in HORIZONS:
            if m + h > SESSION_MIN or (m, h) not in base:
                continue
            if t0 + 60 * h > now:
                h_out[str(h)] = None                      # still to come
                continue
            w = _window(bars, t0, h)
            if len(w) >= 0.9 * h:
                h_out[str(h)] = [round(_range(w), 2), round(base[(m, h)], 2)]
        if not h_out:
            continue
        first = _window(bars, t0, 1)
        spot = first[0][1] if first else None
        w = _window(bars, t0 - 60 * CHECK_EVERY_MIN, CHECK_EVERY_MIN)    # the no-GEX guess's input
        b = base.get((m - CHECK_EVERY_MIN, CHECK_EVERY_MIN))
        prev = [round(_range(w), 2), round(b, 2)] if b and len(w) >= 0.9 * CHECK_EVERY_MIN else None
        g = _at(snaps, t0, 'total_gex')
        if g is not None:
            rows.append({'kind': 'size_regime', 't': t0, 'spot': spot, 'gex': round(g),
                         'predict': 'bigger' if g < 0 else 'smaller', 'h': h_out, 'prev': prev})
        j = bisect_right(ttimes, t0) - 1
        if j >= 0 and t0 - ttimes[j] <= STALE_SEC:
            g = timeline[j][3]
            rows.append({'kind': 'size_price', 't': t0, 'spot': spot, 'gex': round(g),
                         'predict': 'bigger' if g < 0 else 'smaller', 'h': h_out, 'prev': prev})
    return rows


def _first(bars: list, i: int, hit_a, hit_b, minutes: int = max(HORIZONS)):
    """('a' | 'b' | None, minutes taken) scanning bars i.. for `minutes`: whichever test
    passes first (both in one bar: 'b', the conservative side)."""
    t_i = bars[i][0]
    for j in range(i, len(bars)):
        if bars[j][0] - t_i >= 60 * minutes:
            break
        a, b = hit_a(bars[j]), hit_b(bars[j])
        if b:
            return 'b', j - i + 1
        if a:
            return 'a', j - i + 1
    return None, None


def predict_levels(day: str, bars: list, snaps: list, timeline: list) -> list:
    """LEVEL rows for each approach to a gamma level, and RUN rows for each break."""
    rows = []
    if not timeline or not bars:
        return rows
    ttimes = [x[0] for x in timeline]
    armed = {}                                            # strike -> +1 above / -1 below, 0 = spent
    prev_close = bars[0][1]
    for i, (t, o, hi, lo, c) in enumerate(bars):
        j = bisect_right(ttimes, t) - 1
        if j < 0 or t - ttimes[j] > STALE_SEC:
            prev_close = c
            continue
        by_k = timeline[j][2]
        names = {}
        k0 = j
        while k0 >= 0 and t - ttimes[k0] < LABEL_MEMORY_SEC:
            for k, ns in timeline[k0][1].items():
                names.setdefault(k, set()).update(ns)
            k0 -= 1
        for field, label in (('call_wall', 'call wall'), ('put_wall', 'put wall')):
            k = _at(snaps, t, field)
            if k is not None and k in by_k:
                names.setdefault(k, set()).add(label)
        for k, ns in names.items():
            side = armed.get(k)
            if not side:
                if abs(prev_close - k) >= ARM_PTS:
                    armed[k] = side = 1 if prev_close > k else -1
                else:
                    continue
            touched = lo <= k + TOUCH_PTS if side > 0 else hi >= k - TOUCH_PTS
            if not touched:
                continue
            armed[k] = 0
            g = by_k.get(k, 0.0)
            broke = (lambda b, k=k, s=side: b[4] <= k - BREAK_PTS if s > 0 else b[4] >= k + BREAK_PTS)
            held = (lambda b, k=k, s=side: b[4] >= k + HOLD_PTS if s > 0 else b[4] <= k - HOLD_PTS)
            first, mins = _first(bars, i, held, broke)
            outcome = {'a': 'hold', 'b': 'break'}.get(first)
            rows.append({'kind': 'level', 't': t, 'strike': k, 'names': sorted(ns),
                         'from': 'above' if side > 0 else 'below', 'gex': round(g),
                         'predict': 'hold' if g > 0 else 'break', 'first': outcome, 'min': mins,
                         'until': bars[-1][0]})
            if outcome == 'break':
                jb = i + mins - 1                         # the breaking bar
                cb, d = bars[jb][4], -side
                ran = (lambda b, cb=cb, d=d: b[3] <= cb - RUN_PTS if d < 0 else b[2] >= cb + RUN_PTS)
                back = (lambda b, k=k, d=d: b[4] >= k if d < 0 else b[4] <= k)
                r_first, r_mins = (None, None) if jb + 1 >= len(bars) else _first(bars, jb + 1, ran, back)
                rows.append({'kind': 'run', 't': bars[jb][0], 'strike': k, 'names': sorted(ns),
                             'dir': 'down' if d < 0 else 'up', 'gex': round(g),
                             'predict': 'run' if g < 0 else None,
                             'first': {'a': 'run', 'b': 'back'}.get(r_first), 'min': r_mins,
                             'until': bars[-1][0]})
        prev_close = c
    return rows


def predict_pin(day: str, bars: list, snaps: list, snaps0: list, now: float) -> list:
    rows, t_open = [], _open_ts(day)
    done = bars and bars[-1][0] >= t_open + 60 * (SESSION_MIN - 1)
    close = bars[-1][4] if done else None
    for m in PIN_TIMES:
        t0 = t_open + 60 * m
        if t0 > now:
            break
        before = _window(bars, t0 - 60, 1)
        g0 = _at(snaps0, t0, 'total_gex')
        if not before or g0 is None:
            continue
        spot = before[0][4]
        for kind, src, field in (('pin_true', snaps, 'true_pin'), ('pin_0dte', snaps0, 'pin')):
            p = _at(src, t0, field)
            if p is None or abs(spot - p) < PIN_MIN_DIST:
                continue
            rows.append({'kind': kind, 't': t0, 'spot': spot, 'pin': p, 'gex0': round(g0),
                         'predict': 'nearer' if g0 > 0 else None,
                         'close': close, 'nearer': None if close is None else abs(close - p) < abs(spot - p)})
    return rows


_oq_today = {'path': None, 'offset': 0, 'tail': '', 'cols': None, 'q': {}}
_oq_lock = threading.Lock()


def _oq_parse(c: dict, text: str, day: str, final: bool = False):
    lines = text.split('\n')
    c['tail'] = '' if final else lines.pop()          # possibly still being written
    for line in lines:
        parts = line.strip().split(',')
        if len(parts) < 2:
            continue
        if c['cols'] is None:
            c['cols'] = {n: i for i, n in enumerate(parts)}
            continue
        ix = c['cols']
        try:
            if parts[ix['expiry']] != day:
                continue
            bid, ask = float(parts[ix['bid']] or 0), float(parts[ix['ask']] or 0)
            if bid > 0 and ask > bid:
                c['q'].setdefault((parts[ix['side']], float(parts[ix['strike']])), {})[int(float(parts[ix['time']]))] = (bid, ask)
        except (KeyError, IndexError, ValueError):
            continue


def option_quotes(day: str, data_dir: str = DATA_DIR, today: str | None = None) -> dict:
    """{(side, strike): {minute epoch: (bid, ask)}} for that day's 0DTE, from the stream's
    minute bars (option_bars_SPX_<day>, from 2026-10-14). Today's file is read
    incrementally (it grows all session); a finished day is read once, by save."""
    path = day_file(f'option_bars_SPX_{day}.csv', data_dir)
    if not path:
        return {}
    if day != (today or datetime.now(ET).date().isoformat()) or path.endswith('.gz'):
        c = {'tail': '', 'cols': None, 'q': {}}
        with open_text(path) as f:
            _oq_parse(c, f.read(), day, final=True)
        return c['q']
    with _oq_lock:
        c = _oq_today
        size = os.path.getsize(path)
        if c['path'] != path or size < c['offset']:
            c.update(path=path, offset=0, tail='', cols=None, q={})
        if size > c['offset']:
            with open(path, 'rb') as f:
                f.seek(c['offset'])
                chunk = f.read()
                c['offset'] = f.tell()
            _oq_parse(c, c['tail'] + chunk.decode('utf-8', 'replace'), day)
        return c['q']


def trade_levels(day: str, bars: list, by_day: dict, heat: dict, timeline: list, snaps: list, snaps0: list) -> dict:
    """{level: [strike or None per bar]}: each TRADE_LEVELS level as it stood at each bar."""
    times = [b[0] for b in bars]
    out = {k: [None] * len(bars) for k in TRADE_LEVELS}

    def asof(series_t, series_v, key):
        for i, t in enumerate(times):
            j = bisect_right(series_t, t) - 1
            if j >= 0 and t - series_t[j] <= STALE_SEC:
                out[key][i] = series_v[j]

    # gold: the biggest |GEX| strike of each heatmap snapshot (All)
    ht = [int(t) for t in heat.get('times', [])]
    gold = [heat['strikes'][max(range(len(col)), key=lambda n: abs(col[n]))] if col else None for col in heat.get('all', [])]
    asof(ht, gold, 'gold')
    asof([x[0] for x in timeline], [next((k for k, ns in x[1].items() if 'trapdoor' in ns), None) for x in timeline], 'trapdoor')
    for key, rows, field in (('0DTE put wall', snaps0, 'put_wall'), ('put wall', snaps, 'put_wall'), ('pin', snaps, 'pin')):
        out[key] = [_at(rows, t, field) for t in times]
    prior = [d for d in by_day if d < day]
    if prior:
        pc = by_day[max(prior)][-1][4]
        out['prev close'] = [pc] * len(bars)
    return out


def _trade(q: dict, side: str, k: float, t: int, now: float) -> dict | None:
    """The option bought at the ask at minute t: P&L % at the bid 15 / 30 / 60 min later
    (None while still to come), and whether the bid reached TRADE_HIT x paid within 60."""
    quotes = q.get((side, k))
    if not quotes:
        return None
    entry = quotes.get(t) or quotes.get(t - 60)
    if not entry:
        return None
    ask = entry[1]
    out = {'side': side, 'ask': ask}
    for h in HORIZONS:
        if t + 60 * h > now:
            out[str(h)] = None
            continue
        x = quotes.get(t + 60 * h) or quotes.get(t + 60 * (h - 1))
        out[str(h)] = round(100 * (x[0] - ask) / ask, 1) if x else None
    later = [b for tt, (b, _) in quotes.items() if t < tt <= min(t + 3600, now)]
    out['hit'] = (max(later) >= TRADE_HIT * ask) if later and (t + 3600 <= now or max(later) >= TRADE_HIT * ask) else None
    return out


def predict_trades(day: str, bars: list, levels: dict, q: dict, now: float) -> list:
    """TOUCH TRADE rows at each level touch, and the no-level baseline every 15 min."""
    rows, t_open = [], _open_ts(day)
    if not q or not bars:
        return rows
    closes = [b[4] for b in bars]
    for name, L in list(levels.items()) + [(None, None)]:
        last = -10**9
        for i, (t, *_ohl, c) in enumerate(bars):
            m = (t - t_open) // 60
            if not TRADE_FIRST_MIN <= m <= TRADE_LAST_MIN or i < TRADE_LOOKBACK_MIN or t > now:
                continue
            if name is None:                              # baseline: 'from above' = fell over the lookback
                if m % CHECK_EVERY_MIN or closes[i - TRADE_LOOKBACK_MIN] == c:
                    continue
                at, from_above = None, closes[i - TRADE_LOOKBACK_MIN] > c
            else:
                at = L[i]
                if at is None or abs(c - at) > TRADE_TOUCH_PTS or i - last < TRADE_COOLDOWN_MIN:
                    continue
                far = [x - at for x in closes[i - TRADE_LOOKBACK_MIN:i] if abs(x - at) >= TRADE_APPROACH_PTS]
                if not far:
                    continue
                last, from_above = i, far[-1] > 0
            k = 5 * round(c / 5)
            r = {'kind': 'trade', 't': t, 'level': name or 'none', 'at': at, 'spot': c, 'k': k,
                 'from': 'above' if from_above else 'below', 'predict': TRADE_LEVELS.get(name),
                 'bounce': _trade(q, 'call' if from_above else 'put', k, t, now),
                 'break': _trade(q, 'put' if from_above else 'call', k, t, now)}
            if r['bounce'] or r['break']:
                rows.append(r)
    return rows


def day_rows(day: str, by_day: dict | None = None, now: float | None = None,
             data_dir: str = DATA_DIR) -> list:
    """Every prediction for one session, graded as far as the bars allow."""
    by_day = by_day if by_day is not None else sessions()
    bars = by_day.get(day) or []
    if len(bars) < 30:
        return []
    now = now or time.time()
    snaps = snapshots(day, data_dir=data_dir)
    snaps0 = snapshots(day, zero_dte=True, data_dir=data_dir)
    heat = gex_stats.load_heatmap('SPX', day=date.fromisoformat(day), data_dir=data_dir)
    timeline = level_timeline(heat)
    rows = (predict_size(day, bars, baselines(day, by_day), snaps, timeline, now)
            + predict_levels(day, bars, snaps, timeline)
            + predict_pin(day, bars, snaps, snaps0, now)
            + predict_trades(day, bars, trade_levels(day, bars, by_day, heat, timeline, snaps, snaps0),
                             option_quotes(day, data_dir), now))
    for r in rows:
        r['day'] = day
    return rows


# ── grading ────────────────────────────────────────────────────────────────────

def grade(r: dict, h: int | None = None):
    """True / False once decided at horizon `h` minutes (PIN: at the close), None while
    pending or undecided, for rows that make a prediction."""
    k = r['kind']
    if not r.get('predict'):
        return None
    if k.startswith('size'):
        v = (r.get('h') or {}).get(str(h))
        return None if not v else (v[0] > v[1]) == (r['predict'] == 'bigger')
    if k in ('level', 'run'):
        if not r.get('first') or r['min'] > h:
            return None
        return r['first'] == r['predict']
    if k.startswith('pin'):
        return r.get('nearer')
    return None


def status(r: dict, now: float) -> str:
    """'pending' / 'hit' / 'miss' / 'undecided' / 'comparison' for today's list (the
    middle horizon for SIZE, within 60 min for LEVEL / RUN, the close for PIN)."""
    if not r.get('predict'):
        return 'comparison'
    k = r['kind']
    if k == 'trade':                                      # its graded trade made money at 30 min
        v = (r.get(r['predict']) or {}).get('30')
        if v is None:
            return 'pending' if now - r['t'] < 60 * 32 else 'undecided'
        return 'hit' if v > 0 else 'miss'
    if k.startswith('size'):
        v = (r.get('h') or {}).get('30', (r.get('h') or {}).get('15'))
        if v is None:
            return 'pending'
        return 'hit' if grade(r, 30 if '30' in r['h'] else 15) else 'miss'
    if k in ('level', 'run'):
        if r.get('first'):
            return 'hit' if r['first'] == r['predict'] else 'miss'
        return 'pending' if r['until'] - r['t'] < 60 * (max(HORIZONS) - 1) and now - r['t'] < 3600 else 'undecided'
    return 'pending' if r.get('nearer') is None else ('hit' if r['nearer'] else 'miss')


def _rate(hits: int, n: int) -> dict:
    p = hits / n if n else None
    return {'n': n, 'hits': hits, 'rate': None if p is None else round(100 * p),
            'ci': None if p is None else round(196 * (p * (1 - p) / n) ** 0.5)}


def _majority(outcomes: list) -> int | None:
    """% a guess without GEX scores: always the more common outcome of these."""
    if not outcomes:
        return None
    share = sum(outcomes) / len(outcomes)
    return round(100 * max(share, 1 - share))


def summarize(rows: list) -> list:
    """One card per prediction: hit rate per horizon, the no-GEX comparison, details."""
    cards = []
    by = lambda kind: [r for r in rows if r['kind'] == kind]
    days = lambda rs: len({r['day'] for r in rs})

    for kind, title, says in (
            ('size_regime', 'Size · regime',
             'Net GEX negative → the next move is bigger than usual; positive → smaller'),
            ('size_price', 'Size · GEX at price',
             'GEX at the price negative (accelerator) → bigger than usual; positive (brakes) → smaller')):
        rs, cols = by(kind), []
        for h in HORIZONS:
            # graded rows the no-GEX guess can also call (it needs the 15 min before)
            g = [(r, r['h'][str(h)]) for r in rs if (r.get('h') or {}).get(str(h)) and r.get('prev')]
            hits = sum(1 for r, _ in g if grade(r, h))
            guess = [(r['prev'][0] > r['prev'][1]) == (v[0] > v[1]) for r, v in g]
            # where the two disagree, who was right
            split = [grade(r, h) for r, v in g if (r['prev'][0] > r['prev'][1]) != (r['predict'] == 'bigger')]
            cols.append({'label': f'{h} min', **_rate(hits, len(g)),
                         'base': round(100 * sum(guess) / len(guess)) if guess else None,
                         'split': _rate(sum(split), len(split))})
        ratio = lambda neg: [v[0] / v[1] for r in rs for v in [(r.get('h') or {}).get('30')]
                             if v and v[1] and (r['gex'] < 0) == neg]
        neg, pos = ratio(True), ratio(False)
        detail = ' · '.join(x for x in (
            neg and f'negative: 30-min moves {statistics.median(neg):.2f}× usual (n {len(neg)})',
            pos and f'positive: {statistics.median(pos):.2f}× usual (n {len(pos)})') if x)
        sp = cols[1]['split']
        if sp['n']:
            detail += f' · when GEX and recent moves disagreed, GEX was right {sp["rate"]}% (n {sp["n"]}, 30 min)'
        cards.append({'key': kind, 'title': title, 'says': says,
                      'vs': 'no GEX: the last 15 min moved more than usual → the next 15 / 30 / 60 will too',
                      'cols': cols, 'detail': detail, 'days': days(rs)})

    rs = by('level')
    cols = []
    for h in HORIZONS:
        g = [r for r in rs if grade(r, h) is not None]
        cols.append({'label': f'{h} min', **_rate(sum(1 for r in g if grade(r, h)), len(g)),
                     'base': _majority([r['first'] == 'hold' for r in g])})
    dec = [r for r in rs if r.get('first')]
    pos = [r for r in dec if r['gex'] > 0]
    neg = [r for r in dec if r['gex'] <= 0]
    pct = lambda a, b: f'{round(100 * a / b)}%' if b else '—'
    detail = (f'positive-GEX levels held {pct(sum(r["first"] == "hold" for r in pos), len(pos))} (n {len(pos)})'
              f' · negative-GEX levels broke {pct(sum(r["first"] == "break" for r in neg), len(neg))} (n {len(neg)})'
              f' · undecided after 60 min: {len(rs) - len(dec)}')
    cards.append({'key': 'level', 'title': 'Levels · hold or break',
                  'says': 'Price reaches a gamma level: a positive-GEX strike holds, a negative one breaks',
                  'vs': 'always guess the commoner outcome', 'cols': cols, 'detail': detail, 'days': days(rs)})

    rs = by('run')
    cols = []
    for h in HORIZONS:
        run = lambda sub: [r['first'] == 'run' for r in sub if r.get('first') and r['min'] <= h]
        n_ = run([r for r in rs if r['gex'] < 0])
        p_ = run([r for r in rs if r['gex'] >= 0])
        cols.append({'label': f'{h} min', **_rate(sum(n_), len(n_)),
                     'base': round(100 * sum(p_) / len(p_)) if p_ else None})
    cards.append({'key': 'run', 'title': 'Breaks · keep running',
                  'says': f'A negative-GEX level breaks → price runs {RUN_PTS:g} more points before reclaiming it',
                  'vs': 'the same after positive-GEX levels break', 'cols': cols,
                  'detail': f'breaks: {sum(r["gex"] < 0 for r in rs)} through negative-GEX levels, '
                            f'{sum(r["gex"] >= 0 for r in rs)} through positive', 'days': days(rs)})

    for kind, title in (('pin_true', 'Pin · True Pin'), ('pin_0dte', 'Pin · 0DTE pin')):
        rs = [r for r in by(kind) if r.get('nearer') is not None]
        yes = [r['nearer'] for r in rs if r['gex0'] > 0]
        no = [r['nearer'] for r in rs if r['gex0'] <= 0]
        move = [abs(r['spot'] - r['pin']) - abs(r['close'] - r['pin']) for r in rs if r['gex0'] > 0]
        cards.append({'key': kind, 'title': title,
                      'says': 'From 14:00 with 0DTE gamma positive → the close lands nearer the pin than price is then',
                      'vs': 'the same with 0DTE gamma negative',
                      'cols': [{'label': 'at the close', **_rate(sum(yes), len(yes)),
                                'base': round(100 * sum(no) / len(no)) if no else None}],
                      'detail': f'positive 0DTE gamma: the gap to the pin closed by a median '
                                f'{statistics.median(move):+.1f} pts (n {len(move)})' if move else '',
                      'days': days(rs)})

    # touch trades: average P&L % (ask in, bid out) vs the same trade at no level
    rs = by('trade')
    base = [r for r in rs if r['level'] == 'none']
    pnl = lambda sub, trade, h: [x for r in sub for x in [(r.get(trade) or {}).get(str(h))] if x is not None]

    def pnl_cols(sub, base_sub, trade):
        cols = []
        for h in HORIZONS:
            v, b = pnl(sub, trade, h), pnl(base_sub, trade, h)
            cols.append({'label': f'{h} min', 'pnl': True, 'n': len(v),
                         'rate': round(sum(v) / len(v)) if v else None,
                         'ci': round(1.96 * statistics.stdev(v) / len(v) ** 0.5) if len(v) > 1 else 0,
                         'base': round(sum(b) / len(b)) if b else None})
        return cols

    def hit(sub, trade):
        h = [r[trade]['hit'] for r in sub if r.get(trade) and r[trade].get('hit') is not None]
        return f'{round(100 * sum(h) / len(h))}%' if h else '—'

    buys = {'bounce': 'a call if price came down to it, a put if it came up',
            'break': 'a put if price came down to it, a call if it came up'}
    for name, trade in TRADE_LEVELS.items():
        sub = [r for r in rs if r['level'] == name]
        other = 'break' if trade == 'bounce' else 'bounce'
        o30 = pnl(sub, other, 30)
        cards.append({'key': f'trade_{name}', 'title': f'Touch trade · {name}',
                      'says': f'Price reaches the {name} → buy the 0DTE {trade} ({buys[trade]}) at the ask, '
                              f'sell at the bid: average P&L % of what was paid',
                      'vs': f'the same {trade} trade every 15 min at no level',
                      'cols': pnl_cols(sub, base, trade),
                      'detail': f'hit +25% within an hour {hit(sub, trade)} vs {hit(base, trade)} at no level'
                                + (f' · the {other} instead: {sum(o30) / len(o30):+.0f}% at 30 min (n {len(o30)})' if o30 else ''),
                      'days': days(sub)})
    minute = lambda r: (r['t'] - _open_ts(r['day'])) // 60
    mid = [r for r in rs if r['level'] != 'none' and MIDDAY[0] <= minute(r) < MIDDAY[1]]
    mid_base = [r for r in base if MIDDAY[0] <= minute(r) < MIDDAY[1]]
    cards.append({'key': 'trade_midday', 'title': 'Touch trade · midday',
                  'says': f'Any of those levels reached 12:00-14:00 → buy the 0DTE bounce ({buys["bounce"]}): average P&L %',
                  'vs': 'bounce trades every 15 min at no level, 12:00-14:00', 'cols': pnl_cols(mid, mid_base, 'bounce'),
                  'detail': f'hit +25% within an hour {hit(mid, "bounce")} vs {hit(mid_base, "bounce")} at no level',
                  'days': days(mid)})
    return cards


# ── saving finished days, and the page's summary ───────────────────────────────

_saved = {'sig': None, 'rows': []}
_save_lock = threading.Lock()
_building = threading.Event()


def saved_rows(path: str = SAVE_FILE) -> list:
    """This rules version's saved rows (re-read when the file changes)."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    sig = (path, st.st_size, st.st_mtime, RULES_VERSION)
    if _saved['sig'] == sig:
        return _saved['rows']
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get('v') == RULES_VERSION:
                rows.append(r)
    _saved.update(sig=sig, rows=rows)
    return rows


def recorded_days(data_dir: str = DATA_DIR) -> list:
    """Sessions with GEX snapshots recorded."""
    days = set()
    for p in glob.glob(os.path.join(data_dir, 'gex_snapshots_SPX_2*.csv*')):
        days.add(os.path.basename(p)[len('gex_snapshots_SPX_'):][:10])
    return sorted(days)


def save_finished_days(data_dir: str = DATA_DIR, path: str = SAVE_FILE, today: str | None = None) -> int:
    """Grade and append every finished recorded day not saved under RULES_VERSION yet.
    A day with nothing to grade gets a marker row so it isn't redone."""
    today = today or datetime.now(ET).date().isoformat()
    with _save_lock:
        have = {r['day'] for r in saved_rows(path)}
        todo = [d for d in recorded_days(data_dir) if d < today and d not in have]
        if not todo:
            return 0
        by_day = sessions(max_age=0)
        n = 0
        with open(path, 'a', encoding='utf-8') as f:
            for d in todo:
                try:
                    rows = day_rows(d, by_day, now=time.time(), data_dir=data_dir)
                except Exception as e:  # one bad day must not stop the rest (or be redone each poll)
                    print(f'[predictions] {d}: {e}')
                    rows, err = [], str(e)
                else:
                    err = None
                for r in rows or [{'kind': 'none', 'day': d, **({'error': err} if err else {})}]:
                    f.write(json.dumps({'v': RULES_VERSION, **r}, separators=(',', ':')) + '\n')
                n += len(rows)
        print(f'[predictions] saved {len(todo)} day(s), {n} predictions')
        return n


def _save_in_background():
    if _building.is_set():
        return
    _building.set()

    def run():
        try:
            save_finished_days()
        except Exception as e:
            print(f'[predictions] save failed: {e}')
        finally:
            _building.clear()
    threading.Thread(target=run, daemon=True, name='predictions_save').start()


_today = {'at': 0.0, 'day': None, 'rows': []}


def summary(now: float | None = None) -> dict:
    """The page's scoreboard: cards over saved days + today, and today's predictions."""
    now = now or time.time()
    today = datetime.fromtimestamp(now, ET).date().isoformat()
    saved = [r for r in saved_rows() if r['kind'] != 'none' and r['day'] != today]
    have = {r['day'] for r in saved_rows()}
    if any(d < today and d not in have for d in recorded_days()):
        _save_in_background()
    if _today['day'] != today or now - _today['at'] > 50:
        try:
            rows = day_rows(today, now=now)
        except Exception as e:
            print(f'[predictions] today: {e}')
            rows = []
        _today.update(at=now, day=today, rows=rows)
    live = _today['rows']
    listed = sorted((dict(r, status=status(r, now)) for r in live
                     if not (r['kind'] == 'trade' and r['level'] == 'none')), key=lambda r: -r['t'])
    return {'rules': RULES_VERSION, 'cards': summarize(saved + live),
            'days': len({r['day'] for r in saved + live}), 'building': _building.is_set(),
            'today': listed[:80],
            'settings': {'touch': TOUCH_PTS, 'arm': ARM_PTS, 'break': BREAK_PTS, 'hold': HOLD_PTS,
                         'run': RUN_PTS, 'base_sessions': BASE_SESSIONS}}
