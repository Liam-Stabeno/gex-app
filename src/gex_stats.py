"""
gex_stats.py — statistics from the saved GEX history.

Shared by the dashboard (expected move to the close) and tools/pin_backtest.py.

Data used (all under data/, written by background.py):
    gex_snapshots_SPX_<date>.csv   one row per chain refresh: spot, total_gex, levels
    price_history_SPX.csv          1-min candles, for each day's 16:00 close

Public API:
    load_closes()                          -> {date: 16:00 close}
    load_checkpoints(times)                -> DataFrame day, cp, et, spot, total_gex, levels...
    expected_move_table()                  -> DataFrame regime x checkpoint: median, p80, n
    expected_move_now(total_gex, now=None) -> dict for the dashboard
"""
import glob
import os
import threading
import time
from bisect import bisect_right
from datetime import date, datetime, time as dtime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo('America/New_York')
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

CHECKPOINTS = [dtime(10, 0), dtime(11, 0), dtime(12, 0), dtime(13, 0),
               dtime(14, 0), dtime(15, 0), dtime(15, 30)]
MAX_STALE_SEC = 900          # a snapshot older than this at a checkpoint is not used
TABLE_TTL_SEC = 3600         # expected-move table is rebuilt at most hourly

_lock = threading.Lock()
_table_cache = {'built': 0.0, 'table': None, 'days': 0}


def _to_et(local_naive: str) -> datetime:
    """Snapshot timestamps are naive local time (datetime.now() on this machine)."""
    return datetime.fromtimestamp(
        datetime.strptime(local_naive, '%Y-%m-%d %H:%M:%S').timestamp(), ET)


def load_closes(data_dir: str = _DATA_DIR) -> dict:
    """{date: close of the 15:59 ET bar} for days whose history reaches the close."""
    path = os.path.join(data_dir, 'price_history_SPX.csv')
    if not os.path.exists(path):
        return {}
    ph = pd.read_csv(path, usecols=['datetime', 'close'])
    ph['dt'] = pd.to_datetime(ph['datetime'], unit='ms', utc=True).dt.tz_convert(ET)
    ph = ph[ph['dt'].dt.time <= dtime(15, 59)]
    out = {}
    for day, g in ph.groupby(ph['dt'].dt.date):
        if g['dt'].dt.time.max() >= dtime(15, 55):
            out[day] = float(g['close'].iloc[-1])
    return out


def load_checkpoints(times=CHECKPOINTS, data_dir: str = _DATA_DIR) -> pd.DataFrame:
    """The last snapshot at or before each checkpoint, per saved day."""
    rows = []
    for f in sorted(glob.glob(os.path.join(data_dir, 'gex_snapshots_SPX_2*.csv'))):
        day = datetime.strptime(f[-14:-4], '%Y-%m-%d').date()
        try:
            s = pd.read_csv(f)
        except (OSError, pd.errors.ParserError):
            continue
        if s.empty:
            continue
        s['et'] = s['timestamp'].map(_to_et)
        for cp in times:
            target = datetime.combine(day, cp, ET)
            prior = s[s['et'] <= target]
            if prior.empty or (target - prior['et'].iloc[-1]).total_seconds() > MAX_STALE_SEC:
                continue
            r = prior.iloc[-1].to_dict()
            r.update(day=day, cp=cp.strftime('%H:%M'))
            rows.append(r)
    return pd.DataFrame(rows)


def expected_move_table(data_dir: str = _DATA_DIR) -> tuple:
    """(table, n_days): |close - spot| by GEX regime and checkpoint time.

    table index: (regime, cp) with columns median, p80, n.
    """
    closes = load_closes(data_dir)
    cps = load_checkpoints(data_dir=data_dir)
    if cps.empty or not closes:
        return pd.DataFrame(columns=['median', 'p80', 'n']), 0
    cps = cps[cps['day'].isin(closes)].copy()
    cps['move'] = (cps['day'].map(closes) - cps['spot']).abs()
    cps['regime'] = cps['total_gex'].map(lambda g: 'POSITIVE' if g > 0 else 'NEGATIVE')
    table = cps.groupby(['regime', 'cp'])['move'].agg(
        median='median', p80=lambda m: m.quantile(0.8), n='size')
    return table, int(cps['day'].nunique())


def expected_move_now(total_gex: float, now: datetime | None = None,
                      data_dir: str = _DATA_DIR) -> dict | None:
    """Typical distance from now to the close for the current GEX regime.

    Uses the latest checkpoint at or before now (10:00 before 10:00). Returns
    None outside market hours or without enough history.
    """
    now = now or datetime.now(ET)
    if now.weekday() >= 5 or not (dtime(9, 30) <= now.time() < dtime(16, 0)):
        return None
    with _lock:
        if _table_cache['table'] is None or time.time() - _table_cache['built'] > TABLE_TTL_SEC:
            _table_cache['table'], _table_cache['days'] = expected_move_table(data_dir)
            _table_cache['built'] = time.time()
        table, days = _table_cache['table'], _table_cache['days']
    regime = 'POSITIVE' if total_gex > 0 else 'NEGATIVE'
    past = [cp for cp in CHECKPOINTS if cp <= now.time()] or [CHECKPOINTS[0]]
    key = (regime, past[-1].strftime('%H:%M'))
    if key not in table.index or table.loc[key, 'n'] < 5:
        return None
    r = table.loc[key]
    return {'regime': regime, 'from': key[1], 'median': round(float(r['median']), 1),
            'p80': round(float(r['p80']), 1), 'n': int(r['n']), 'days': days}


_hm_lock = threading.Lock()
_hm_cache: dict = {}   # path -> {'offset', 'tail', 'snaps': [(t, spot, {strike: [all, odte]})]}
# A finished (gzipped) day never changes: load_heatmap keeps its result, a few hundred KB,
# rather than the raw snapshots (~15 MB each). The live chart loads every day it shows.
_hm_done: dict = {}    # (gz path, size, mtime, band, rebuilt file sig) -> load_heatmap result
HM_DONE_MAX = 20
_hm_rebuilt_cache: dict = {}   # rebuilt file path -> ((size, mtime), snapshots); re-read if rebuilt again


@lru_cache(maxsize=64)
def week_end(day_iso: str) -> str:
    """That week's Friday (the day itself from Friday on)."""
    d = date.fromisoformat(day_iso)
    return (d + timedelta(days=max(0, 4 - d.weekday()))).isoformat()


def _hm_parse(line: bytes, day_iso: str, exp: str | None = None):
    """One grid snapshot line -> (epoch, spot, {strike: [all, odte, week]}), summed over
    expiries; week = expirations through that week's Friday. With `exp`, that one
    expiration only: (epoch, spot, {strike: gex}).

    Where the line has 'live0' (the live loop's 0DTE, recorded from 2026-10-29), today's
    0DTE is that instead of the chain's, strike by strike (the chain's stays in the file)."""
    import json
    try:
        s = json.loads(line)
    except ValueError:
        return None
    exps = s.get('exp', [])
    live0 = s.get('live0') or []
    if exp is not None:
        ei = exps.index(exp) if exp in exps else -1
        one = {}
        for k, exp_i, _c, _p, gex in s.get('rows', []):
            if exp_i == ei:
                one[k] = one.get(k, 0.0) + gex
        if exp == day_iso:
            one.update({k: g for k, g in live0})
        return int(datetime.fromisoformat(s['ts']).timestamp()), s.get('spot'), one
    today_i = exps.index(day_iso) if day_iso in exps else -1
    fri = week_end(day_iso)
    week_i = {i for i, e in enumerate(exps) if e <= fri}
    by_k = {}
    for k, exp_i, _c, _p, gex in s.get('rows', []):
        cell = by_k.setdefault(k, [0.0, 0.0, 0.0])
        cell[0] += gex
        if exp_i == today_i:
            cell[1] += gex
        if exp_i in week_i:
            cell[2] += gex
    for k, g in live0:
        cell = by_k.setdefault(k, [0.0, 0.0, 0.0])
        d = g - cell[1]                                   # live minus the chain's 0DTE
        cell[0] += d
        cell[1] = g
        cell[2] += d
    return int(datetime.fromisoformat(s['ts']).timestamp()), s.get('spot'), by_k


def _hm_snapshots(path: str, day_iso: str, exp: str | None = None) -> list:
    """Parsed snapshots for a day file (with `exp`: that one expiration). Today's plain
    file is cached and read incrementally (only lines appended since the last call):
    with a snapshot every minute it grows to ~15 MB, so re-reading it every 30-60 s
    would cost real CPU. A gzipped finished day is parsed in full; load_heatmap caches
    its result."""
    import gzip
    path = archived(path)
    if path.endswith('.gz'):
        with gzip.open(path, 'rb') as f:
            return [x for x in (_hm_parse(l, day_iso, exp) for l in f if l.strip()) if x]
    st = os.stat(path)
    key = path if exp is None else (path, exp)
    with _hm_lock:
        c = _hm_cache.get(key)
        if not c or st.st_size < c['offset']:            # new or rewritten: start over
            c = _hm_cache[key] = {'offset': 0, 'tail': b'', 'snaps': []}
        if st.st_size > c['offset']:
            with open(path, 'rb') as f:
                f.seek(c['offset'])
                chunk = c['tail'] + f.read()
                c['offset'] = f.tell()
            lines = chunk.split(b'\n')
            c['tail'] = lines.pop()                       # last line, possibly still being written
            c['snaps'].extend(x for x in (_hm_parse(l, day_iso, exp) for l in lines if l.strip()) if x)
            if c['tail'].strip():                         # complete JSON without a final newline:
                x = _hm_parse(c['tail'], day_iso, exp)    # a half-written line can't parse
                if x:
                    c['snaps'].append(x)
                    c['tail'] = b''
        return c['snaps']


def _hm_rebuilt(path: str, day_iso: str, exp: str | None = None) -> list:
    """Snapshots tools/rebuild_gex_grid.py wrote for a day, parsed once per version."""
    path = archived(path)
    st = os.stat(path)
    sig = (st.st_size, st.st_mtime)
    with _hm_lock:
        hit = _hm_rebuilt_cache.get((path, exp))
        if hit and hit[0] == sig:
            return hit[1]
    with open_text(path) as f:
        snaps = [x for x in (_hm_parse(l, day_iso, exp) for l in f if l.strip()) if x]
    with _hm_lock:
        _hm_rebuilt_cache[(path, exp)] = (sig, snaps)
    return snaps


FULL_NEAR_DAYS = 30       # the live chain's window (gex.fetch_option_chain): Full adds what's past it
_full_today: dict = {'path': None, 'offset': 0, 'tail': b'', 'snaps': []}


def _full_far(line: bytes, day_iso: str):
    """(epoch, {strike: net GEX}) of one full-chain snapshot's expirations past the live
    chain's FULL_NEAR_DAYS: what the heatmap's All leaves out."""
    import json
    import key_dates
    try:
        snap = json.loads(line)
    except ValueError:
        return None                                       # a line still being written
    cutoff = (date.fromisoformat(day_iso) + timedelta(days=FULL_NEAR_DAYS)).isoformat()
    far = {}
    for exp, cells in key_dates.strike_gex(snap).items():
        if exp > cutoff:
            for k, g in cells.items():
                far[k] = far.get(k, 0.0) + g
    return int(datetime.fromisoformat(snap['ts']).timestamp()), far


def _full_snapshots(sym: str, day_iso: str, data_dir: str = _DATA_DIR) -> list:
    """[(epoch, {strike: GEX past FULL_NEAR_DAYS})] for each full-chain snapshot of the day
    (gex_full_<sym>_<date>.jsonl, every 15 min, from 2026-10-15). Today's file is read
    incrementally: each snapshot line is ~600 KB."""
    path = day_file(f'gex_full_{sym}_{day_iso}.jsonl', data_dir)
    if not path:
        return []
    if path.endswith('.gz'):                              # a finished day: load_heatmap keeps its result
        with open_text(path) as f:
            return [x for x in (_full_far(l, day_iso) for l in f if l.strip()) if x]
    with _hm_lock:
        c = _full_today
        size = os.path.getsize(path)
        if c['path'] != path or size < c['offset']:
            c.update(path=path, offset=0, tail=b'', snaps=[])
        if size > c['offset']:
            with open(path, 'rb') as f:
                f.seek(c['offset'])
                chunk = c['tail'] + f.read()
                c['offset'] = f.tell()
            lines = chunk.split(b'\n')
            c['tail'] = lines.pop()
            c['snaps'].extend(x for x in (_full_far(l, day_iso) for l in lines if l.strip()) if x)
        return list(c['snaps'])


def load_heatmap(sym: str = 'SPX', day=None, band: float = 250.0,
                 data_dir: str = _DATA_DIR, rebuilt: bool = False, red: int = 0) -> dict:
    """Net GEX per strike over time, from gex_grid_<sym>_<date>.jsonl (one snapshot per
    chain refresh, about every minute; every 5 min before 2026-10-07).

    Returns {'times': [epoch s], 'spots', 'strikes': [...], 'all': [[gex per strike] per
    time], 'odte': [...], 'week': [...] (expirations through Friday), 'full': [...] (All plus
    every expiration past FULL_NEAR_DAYS, from the latest full-chain snapshot; empty on days
    without one), 'rebuilt_until'} for
    strikes within `band` pts of the day's spot
    range. Positive = dealers long gamma (hedging dampens moves); negative = amplifies.

    rebuilt=True puts first the minutes tools/rebuild_gex_grid.py rebuilt for when the app
    wasn't running (gex_grid_rebuilt_<sym>_<date>.jsonl, before the first recorded
    snapshot); 'rebuilt_until' is the last rebuilt column's time. The chart asks for
    them; the stats keep to recorded data.

    red=n adds the next n red days' expirations (key_dates: opex, month end, FOMC, CPI,
    jobs) as 'x:<date>' columns, each that one expiration's GEX, and lists them in 'red'.
    """
    day = day or datetime.now(ET).date()
    red_list = []
    if red:
        import key_dates
        red_list = [{'date': d, 'tags': tags, 'key': f'x:{d}'} for d, tags in key_dates.next_red_days(red)]
    exps = tuple(r['date'] for r in red_list)
    path = day_file(f'gex_grid_{sym}_{day.isoformat()}.jsonl', data_dir)
    # no columns yet (pre-market), but still the red days: the page builds their buttons from it
    empty = {'times': [], 'spots': [], 'strikes': [], 'all': [], 'odte': [], 'week': [], 'full': [], 'rebuilt_until': None,
             **({'red': red_list} if red else {}), **{f'x:{e}': [] for e in exps}}
    if not path:
        return empty
    rb_path = day_file(f'gex_grid_rebuilt_{sym}_{day.isoformat()}.jsonl', data_dir) if rebuilt else None
    done_key = None
    if path.endswith('.gz'):
        st = os.stat(path)
        rb_st = os.stat(rb_path) if rb_path else None
        done_key = (path, st.st_size, st.st_mtime, band, rb_st and (rb_path, rb_st.st_size, rb_st.st_mtime), exps)
        with _hm_lock:
            for key in [k for k in _hm_cache if (k if isinstance(k, str) else k[0]) == path[:-3]]:
                _hm_cache.pop(key)                       # the day's plain file was archived
            if done_key in _hm_done:
                return _hm_done[done_key]
    # Session only: until 2026-10-08 the grid also ran 16:00-16:15, when the expired
    # 0DTE still showed huge gamma (a fake -351M wall). Those lines stay in the file.
    snaps = [x for x in _hm_snapshots(path, day.isoformat())
             if datetime.fromtimestamp(x[0], ET).time() < dtime(16, 0)]
    if not snaps:
        return empty
    early = [x for x in _hm_rebuilt(rb_path, day.isoformat()) if x[0] < snaps[0][0]] if rb_path else []
    snaps = early + snaps
    spots = [sp for _, sp, _ in snaps if sp]
    lo, hi = min(spots) - band, max(spots) + band
    full = _full_snapshots(sym, day.isoformat(), data_dir)
    strikes = sorted({k for _, _, by_k in snaps for k in by_k if lo <= k <= hi}
                     | {k for _, far in full for k in far if lo <= k <= hi})
    out = {'times': [], 'spots': [], 'strikes': strikes, 'all': [], 'odte': [], 'week': [], 'full': [],
           'rebuilt_until': early[-1][0] if early else None}
    full_t = [t for t, _ in full]
    for t, sp, by_k in snaps:
        out['times'].append(t)
        out['spots'].append(sp)
        out['all'].append([round(by_k[k][0]) if k in by_k else 0 for k in strikes])
        out['odte'].append([round(by_k[k][1]) if k in by_k else 0 for k in strikes])
        out['week'].append([round(by_k[k][2]) if k in by_k else 0 for k in strikes])
        if full:                                          # All + the latest full chain's later expirations
            far = full[max(bisect_right(full_t, t) - 1, 0)][1]        # (before the first: the first)
            out['full'].append([round((by_k[k][0] if k in by_k else 0) + far.get(k, 0)) for k in strikes])
    for e in exps:                                        # one expiration per red day
        one = {t: g for t, _, g in _hm_snapshots(path, day.isoformat(), e)}
        if early:
            one.update({t: g for t, _, g in _hm_rebuilt(rb_path, day.isoformat(), e)})
        out[f'x:{e}'] = [[round(one.get(t, {}).get(k, 0)) for k in strikes] for t in out['times']]
    out['red'] = red_list
    if done_key:
        with _hm_lock:
            _hm_done[done_key] = out
            while len(_hm_done) > HM_DONE_MAX:            # oldest first
                _hm_done.pop(next(iter(_hm_done)))
    return out


# ── Trading levels from one GEX-by-strike profile ──────────────────────────────
# Thresholds are fractions of the profile's largest |GEX| (or largest positive).
NEG_MIN        = 0.10   # trapdoor / squeeze: net GEX <= -10% of max |GEX| (was 3%: small
                        # negatives kept swapping places, 7745/7700/7720 on 2026-10-06)
SWITCH_MARGIN  = 0.15   # a level only moves when the new strike beats the current one by 15%
SUPPORT_MIN    = 0.25   # support / resistance must be >= 25% of the largest positive strike
SR_MIN_PTS     = 7.5    # ...and at least this far from spot (skip the strikes price sits on)
AIR_MAX        = 0.05   # air pocket: |GEX| under 5% of max for at least AIR_MIN_PTS
AIR_MIN_PTS    = 10
LEVEL_BAND_PTS = 150    # only look this far from spot
BRAKES_STRONG, BRAKES_WEAK = 0.50, 0.20


def gamma_levels(strikes: list, gex: list, spot: float, n_walls: int = 3, prev: dict | None = None) -> dict:
    """Walls, trapdoor / squeeze, support / resistance, air pockets and the brakes
    reading at spot, from net GEX per strike (positive = dealers long gamma).

    trapdoor : nearest strike below spot where net GEX turns clearly negative —
               dealers start selling into the drop, moves can speed up
    squeeze  : the same above spot
    support / resistance : the biggest positive-GEX strike below / above spot (within
               LEVEL_BAND_PTS, at least SR_MIN_PTS away) — the big walls price has to get through
    air_pockets : runs of near-zero GEX near spot, where price travels easily
    brakes   : GEX at spot vs the largest positive strike (strong / moderate / weak,
               or 'accelerator' when negative)

    prev: the previous snapshot's {support, resistance, trapdoor, squeeze} strikes.
    A previous level is kept while it still qualifies, unless (support / resist) a
    new strike is more than SWITCH_MARGIN bigger — near-equal strikes otherwise
    swap every snapshot.
    """
    pts = sorted((float(k), float(g)) for k, g in zip(strikes, gex) if abs(float(k) - spot) <= LEVEL_BAND_PTS)
    if not pts:
        return {}
    max_abs = max(abs(g) for _, g in pts) or 1.0
    max_pos = max([g for _, g in pts if g > 0], default=0.0) or max_abs
    below = [p for p in reversed(pts) if p[0] < spot]
    above = [p for p in pts if p[0] > spot]
    lvl = lambda p: {'strike': p[0], 'gex': p[1], 'dist': round(p[0] - spot, 2)} if p else None
    first = lambda seq, cond: next((p for p in seq if cond(p[1])), None)
    # biggest positive strike in a set, if it's big enough to matter
    biggest = lambda seq: max((p for p in seq if p[1] >= SUPPORT_MIN * max_pos), key=lambda p: p[1], default=None)

    walls = sorted(pts, key=lambda p: -abs(p[1]))[:n_walls]
    air, run = [], []
    for k, g in pts + [(None, None)]:
        if k is not None and abs(g) < AIR_MAX * max_abs:
            run.append(k)
            continue
        if run and run[-1] - run[0] >= AIR_MIN_PTS:
            air.append([run[0], run[-1]])
        run = []

    # GEX at spot: linear between the two strikes around it
    lo = below[0] if below else None
    hi = above[0] if above else None
    if lo and hi:
        w = (spot - lo[0]) / (hi[0] - lo[0])
        g_spot = lo[1] * (1 - w) + hi[1] * w
    else:
        g_spot = (lo or hi)[1]
    ratio = g_spot / max_pos
    label = ('accelerator' if g_spot < 0 else 'strong' if ratio >= BRAKES_STRONG
             else 'moderate' if ratio >= BRAKES_WEAK else 'weak')

    by_k = dict(pts)
    sup_c = [p for p in below if spot - p[0] >= SR_MIN_PTS]
    res_c = [p for p in above if p[0] - spot >= SR_MIN_PTS]
    picks = {
        'trapdoor': first(below, lambda g: g <= -NEG_MIN * max_abs),
        'squeeze': first(above, lambda g: g <= -NEG_MIN * max_abs),
        'support': biggest(sup_c),
        'resistance': biggest(res_c),
    }
    if prev:
        def still(key, k):
            g = by_k.get(k)
            if g is None:
                return False
            if key == 'trapdoor':
                return k < spot and g <= -NEG_MIN * max_abs
            if key == 'squeeze':
                return k > spot and g <= -NEG_MIN * max_abs
            side_ok = (spot - k >= SR_MIN_PTS) if key == 'support' else (k - spot >= SR_MIN_PTS)
            return side_ok and g >= SUPPORT_MIN * max_pos
        for key, new in picks.items():
            k = prev.get(key)
            if k is None or not still(key, k) or (new and new[0] == k):
                continue
            if key in ('support', 'resistance') and new and new[1] > by_k[k] * (1 + SWITCH_MARGIN):
                continue                      # clearly bigger wall: move
            picks[key] = (k, by_k[k])

    return {
        'spot': spot,
        'walls': [lvl(p) for p in walls],
        'trapdoor': lvl(picks['trapdoor']),
        'squeeze': lvl(picks['squeeze']),
        'support': lvl(picks['support']),
        'resistance': lvl(picks['resistance']),
        'air_pockets': air,
        'brakes': {'label': label, 'pct': round(100 * ratio), 'gex': g_spot},
    }


_gl_history: dict = {}   # (sym, day, mode, data_dir) -> (columns seen, last time, history, prev)


def current_gamma_levels(spot: float | None, mode: str = 'all', sym: str = 'SPX',
                         data_dir: str = _DATA_DIR, day=None, live: dict | None = None) -> dict | None:
    """gamma_levels() on the latest snapshot of a day's heatmap (default today), with a
    history of each snapshot's levels for the chart's trails. For a past day, spot
    defaults to that day's last snapshot.

    live: today's 0DTE GEX per strike now (background.live_0dte), in place of the latest
    snapshot's 0DTE part, so the levels move with the stream (every ~2 s) and agree with
    the chart's medals and key strikes, which read the same numbers."""
    # 'x:<date>': one red day's expiration (the next two are offered)
    h = load_heatmap(sym, day=day, data_dir=data_dir, red=2 if mode.startswith('x:') else 0)
    if not h['times'] or not (mode in ('all', 'odte', 'week', 'full') or (mode.startswith('x:') and mode in h)):
        return None
    if not h[mode]:                                       # Full on a day without a full-chain snapshot
        return None
    if spot is None:
        spot = next((s for s in reversed(h['spots']) if s), None)
        if spot is None:
            return None
    # the same levels for every snapshot of the day, in order, each one sticky to the one
    # before — so the chart can trail them. Kept until a new snapshot arrives: the page
    # asks every 2 s and only the last pick changes between snapshots.
    key = (sym, day, mode, str(data_dir))
    hit = _gl_history.get(key)
    if hit and hit[0] == len(h['times']) and hit[1] == h['times'][-1]:
        history, prev = hit[2], hit[3]
    else:
        history, prev = [], None
        for t, col, sp in zip(h['times'], h[mode], h['spots']):
            if not sp:
                continue
            L = gamma_levels(h['strikes'], col, float(sp), prev=prev)
            prev = {k: (L[k]['strike'] if L.get(k) else None) for k in ('support', 'resistance', 'trapdoor', 'squeeze')}
            history.append({'time': t, **prev})
        if len(_gl_history) > 32:
            _gl_history.clear()
        _gl_history[key] = (len(h['times']), h['times'][-1], history, prev)

    col, all_now = h[mode][-1], h['all'][-1]
    if live:
        lv = [live.get(k) for k in h['strikes']]
        odte = h['odte'][-1]
        swap = lambda c: [x - o + v if v is not None else x for x, o, v in zip(c, odte, lv)]
        today = (day or datetime.now(ET).date()).isoformat()
        if mode in ('all', 'week', 'full'):
            col = swap(col)
        elif mode in ('odte', f'x:{today}'):
            col = [v if v is not None else x for x, v in zip(col, lv)]
        all_now = swap(all_now)
    out = gamma_levels(h['strikes'], col, spot, prev=prev)
    if out:
        out['history'] = history
        # air pockets are about total gamma: 0DTE alone looks empty away from spot
        # even where other expiries hold plenty
        if mode not in ('all', 'full'):
            out['air_pockets'] = gamma_levels(h['strikes'], all_now, spot).get('air_pockets', [])
        out.update(mode=mode, as_of=h['times'][-1], live=bool(live))
    return out


# ── Long-term storage ──────────────────────────────────────────────────────────
# Day files are kept forever. Once a day is over, the large ones are gzipped
# (~10x smaller); readers accept either form. A one-row-per-day summary CSV
# (daily_summary_<sym>.csv) is the quick way to track things day to day.
ARCHIVE_PATTERNS = ('gex_grid_{sym}_*.jsonl', 'rolling_profile_{sym}_*.jsonl', 'gex_watchlist_{sym}_*.csv',
                    'gex_grid_rebuilt_{sym}_*.jsonl',  # tools/rebuild_gex_grid.py
                    'gex_full_{sym}_*.jsonl',          # full chain, every expiration (market_recorder)
                    # market_recorder (model-training data)
                    'option_ticks_{sym}_*.csv', 'option_bars_{sym}_*.csv', 'option_chain_{sym}_*.csv',
                    'es_ticks_*.csv', 'es_book_*.csv', 'internals_*.csv',
                    # option_history (Schwab trade bars per contract)
                    'option_trade_bars_{sym}_*.csv', 'option_trade_bars_5m_{sym}_*.csv',
                    'option_daily_bars_{sym}_*.csv')


def day_file(name: str, data_dir: str = _DATA_DIR) -> str | None:
    """Path of a day file, plain or gzipped (None if neither exists)."""
    plain = os.path.join(data_dir, name)
    for p in (plain, plain + '.gz'):
        if os.path.exists(p):
            return p
    return None


def archived(path: str) -> str:
    """`path`, or its .gz once archive_old_files has gzipped it (it can do that between a
    reader looking the file up and opening it: the archive runs at startup, while the
    page's first requests come in)."""
    return path if path.endswith('.gz') or os.path.exists(path) else path + '.gz'


def open_text(path: str):
    import gzip
    return gzip.open(path, 'rt', encoding='utf-8') if path.endswith('.gz') else open(path, encoding='utf-8')


def archive_old_files(sym: str = 'SPX', today=None, data_dir: str = _DATA_DIR) -> list:
    """Gzip finished day files (dated before today). Verified before the original goes."""
    import gzip, re, shutil
    today = today or datetime.now(ET).date()
    done = []
    for pat in ARCHIVE_PATTERNS:
        for path in glob.glob(os.path.join(data_dir, pat.format(sym=sym))):
            m = re.search(r'(\d{4}-\d{2}-\d{2})', os.path.basename(path))
            if not m or datetime.strptime(m.group(1), '%Y-%m-%d').date() >= today:
                continue
            gz, tmp = path + '.gz', path + '.gz.tmp'
            with open(path, 'rb') as src, gzip.open(tmp, 'wb', compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            with gzip.open(tmp, 'rb') as chk:                 # read back fully before deleting
                n = 0
                for block in iter(lambda: chk.read(1 << 20), b''):
                    n += len(block)
            if n != os.path.getsize(path):
                os.remove(tmp)
                continue
            os.replace(tmp, gz)
            os.remove(path)
            done.append(os.path.basename(gz))
    return done


SUMMARY_FIELDS = ['date', 'open', 'high', 'low', 'close', 'range', 'change',
                  'regime_open', 'regime_close', 'total_gex_close_m',
                  'flip', 'put_wall', 'call_wall', 'pin_lt', 'true_pin', 'pin_0dte',
                  'support', 'resistance', 'trapdoor', 'squeeze', 'brakes_close',
                  'wall1', 'wall1_m', 'wall2', 'wall2_m', 'wall3', 'wall3_m',
                  'biggest_wall_of_day', 'biggest_wall_m', 'snapshots', 'grid_snapshots']


def build_daily_summary(day, sym: str = 'SPX', data_dir: str = _DATA_DIR) -> dict | None:
    """One row describing a finished day: prices, regime, levels at the close, walls."""
    snap_path = os.path.join(data_dir, f'gex_snapshots_{sym}_{day.isoformat()}.csv')
    if not os.path.exists(snap_path):
        return None
    s = pd.read_csv(snap_path)
    if s.empty:
        return None
    s['et'] = s['timestamp'].map(_to_et)
    sess = s[s['et'].map(lambda t: dtime(9, 30) <= t.time() <= dtime(16, 0))]
    use = sess if not sess.empty else s
    first, last = use.iloc[0], use.iloc[-1]

    def num(v):
        try:
            return round(float(v), 2) if pd.notna(v) and v != '' else ''
        except (TypeError, ValueError):
            return ''

    row = {k: '' for k in SUMMARY_FIELDS}
    row.update(date=day.isoformat(),
               regime_open='POSITIVE' if first['total_gex'] > 0 else 'NEGATIVE',
               regime_close='POSITIVE' if last['total_gex'] > 0 else 'NEGATIVE',
               total_gex_close_m=round(float(last['total_gex']) / 1e6),
               flip=num(last.get('flip_level')), put_wall=num(last.get('put_wall')),
               call_wall=num(last.get('call_wall')), pin_lt=num(last.get('pin')),
               true_pin=num(last.get('true_pin')), snapshots=len(s))

    p0 = os.path.join(data_dir, f'gex_0dte_snapshots_{sym}_{day.isoformat()}.csv')
    if os.path.exists(p0):
        s0 = pd.read_csv(p0)
        if not s0.empty:
            row['pin_0dte'] = num(s0['pin'].iloc[-1])

    ph_path = os.path.join(data_dir, f'price_history_{sym}.csv')
    if os.path.exists(ph_path):
        ph = pd.read_csv(ph_path)
        ph['dt'] = pd.to_datetime(ph['datetime'], unit='ms', utc=True).dt.tz_convert(ET)
        t = ph['dt'].dt.time
        d = ph[(ph['dt'].dt.date == day) & (t >= dtime(9, 30)) & (t <= dtime(15, 59))]
        if not d.empty:
            o, c = float(d['open'].iloc[0]), float(d['close'].iloc[-1])
            hi, lo = float(d['high'].max()), float(d['low'].min())
            row.update(open=o, high=hi, low=lo, close=c, range=round(hi - lo, 2), change=round(c - o, 2))

    h = load_heatmap(sym, day=day, data_dir=data_dir)
    if h['times']:
        row['grid_snapshots'] = len(h['times'])
        spot = float(row['close'] or last['spot'])
        L = gamma_levels(h['strikes'], h['all'][-1], spot)
        for k in ('support', 'resistance', 'trapdoor', 'squeeze'):
            row[k] = L[k]['strike'] if L.get(k) else ''
        row['brakes_close'] = L.get('brakes', {}).get('label', '')
        for i, w in enumerate(L.get('walls', [])[:3], 1):
            row[f'wall{i}'], row[f'wall{i}_m'] = w['strike'], round(w['gex'] / 1e6)
        best = max(((abs(g), k, g) for col in h['all'] for k, g in zip(h['strikes'], col)), default=None)
        if best:
            row['biggest_wall_of_day'], row['biggest_wall_m'] = best[1], round(best[2] / 1e6)
    return row


def write_daily_summary(day, sym: str = 'SPX', data_dir: str = _DATA_DIR) -> dict | None:
    """Insert or replace the day's row in daily_summary_<sym>.csv (sorted by date)."""
    row = build_daily_summary(day, sym, data_dir)
    if not row:
        return None
    path = os.path.join(data_dir, f'daily_summary_{sym}.csv')
    df = pd.read_csv(path, dtype=str) if os.path.exists(path) else pd.DataFrame(columns=SUMMARY_FIELDS)
    df = df[df['date'] != row['date']]
    df = pd.concat([df, pd.DataFrame([row]).astype(str)], ignore_index=True).sort_values('date')
    tmp = path + '.tmp'
    df.reindex(columns=SUMMARY_FIELDS).to_csv(tmp, index=False)
    os.replace(tmp, path)
    return row


def summary_days_missing(sym: str = 'SPX', data_dir: str = _DATA_DIR) -> list:
    """Finished days that have snapshots but no summary row yet."""
    path = os.path.join(data_dir, f'daily_summary_{sym}.csv')
    have = set(pd.read_csv(path, dtype=str)['date']) if os.path.exists(path) else set()
    today = datetime.now(ET).date()
    days = sorted({datetime.strptime(f[-14:-4], '%Y-%m-%d').date()
                   for f in glob.glob(os.path.join(data_dir, f'gex_snapshots_{sym}_2*.csv'))})
    return [d for d in days if d < today and d.isoformat() not in have]


def history_days(sym: str = 'SPX', data_dir: str = _DATA_DIR) -> list:
    """Days that can be replayed, newest first, with what each one has saved."""
    days = {}
    for f in glob.glob(os.path.join(data_dir, f'gex_snapshots_{sym}_2*.csv')):
        days.setdefault(f[-14:-4], {})['levels'] = True
    for f in glob.glob(os.path.join(data_dir, f'gex_grid_{sym}_2*.jsonl*')):
        days.setdefault(os.path.basename(f)[len(f'gex_grid_{sym}_'):][:10], {})['heatmap'] = True
    for f in glob.glob(os.path.join(data_dir, 'volume_split_ES_2*.csv')):
        days.setdefault(f[-14:-4], {})['es_split'] = True
    for f in glob.glob(os.path.join(data_dir, 'flow_alerts_2*.json')):
        days.setdefault(f[-15:-5], {})['flow'] = True
    return [{'date': d, **{k: v.get(k, False) for k in ('levels', 'heatmap', 'es_split', 'flow')}}
            for d, v in sorted(days.items(), reverse=True)]


def heatmap_reliability(sym: str = 'SPX', data_dir: str = _DATA_DIR) -> dict:
    """From the scorecard: did bright heatmap bands slow price, per gamma regime (median
    speed ratio over days, >1 = slower in bright bands); each day's share of the session
    in negative gamma; and charm flow's live hit record."""
    out = {'pos': None, 'neg': None, 'days': {}, 'charm': None}
    path = os.path.join(data_dir, f'scorecard_{sym}.csv')
    if not os.path.exists(path):
        return out
    df = pd.read_csv(path, dtype=str)
    num = lambda c: pd.to_numeric(df[c], errors='coerce') if c in df else pd.Series(dtype=float)
    for tag in ('pos', 'neg'):
        v = num(f'speed_ratio_{tag}').dropna()
        if len(v):
            out[tag] = {'ratio': round(float(v.median()), 2), 'n': int(len(v))}
    for d, x in zip(df['date'], num('neg_share')):
        if pd.notna(x):
            out['days'][d] = {'neg_share': float(x)}
    calls = num('charm_calls').sum()
    if calls:
        out['charm'] = {'hits': int(num('charm_hits').sum()), 'calls': int(calls)}
    return out


# ── Daily scorecard: did the levels work? ──────────────────────────────────────
# One row per day in data/scorecard_<sym>.csv, written after the close. Over weeks
# it shows which levels hold, which pins land near the close, whether bright heatmap
# bands really slow price, and whether charm flow called the move into the close.
TOUCH_PTS = 3.0     # a wall is "touched" when price comes within this many points
BREAK_PTS = 2.0     # ...and "broken" when a 1-min close goes this far through it

SCORECARD_FIELDS = [
    'date', 'close', 'high', 'low', 'regime_close',
    'resist', 'resist_touch', 'resist_break', 'resist_gap_at_high',
    'support', 'support_touch', 'support_break', 'support_gap_at_low',
    'call_wall', 'call_wall_touch', 'call_wall_break',
    'put_wall', 'put_wall_touch', 'put_wall_break',
    'trapdoor', 'trapdoor_break',
    'pin_lt_dist', 'true_pin_dist_1400', 'pin_0dte_dist_1400',
    'true_pin_dist_1500', 'pin_0dte_dist_1500', 'true_pin_dist_1530', 'pin_0dte_dist_1530',
    'em_1400_move', 'em_1400_median', 'em_1400_p80', 'em_1400_in_median', 'em_1400_in_p80',
    'speed_dim', 'speed_bright', 'speed_ratio', 'speed_hours_slower', 'speed_hours',
    'neg_share', 'speed_ratio_pos', 'speed_ratio_neg',
    'charm_hits', 'charm_calls', 'charm_sign',
]


def wall_reaction(px: pd.DataFrame, level: float, side: str) -> dict:
    """Touch / break / closest gap for a wall above ('up') or below ('down') price."""
    if level is None or px.empty:
        return {'touch': '', 'break': '', 'gap': ''}
    if side == 'up':
        gap = level - float(px['high'].max())
        broke = bool((px['close'] > level + BREAK_PTS).any())
    else:
        gap = float(px['low'].min()) - level
        broke = bool((px['close'] < level - BREAK_PTS).any())
    return {'touch': int(gap <= TOUCH_PTS), 'break': int(broke), 'gap': round(gap, 2)}


def _speed_ratio(q: pd.DataFrame, min_rows: int = 60):
    """Mean 1-min move in the lowest-GEX third over the highest-GEX third (>1 = price
    slower where GEX is high). None with too few minutes or no spread in GEX."""
    if len(q) < min_rows:
        return None
    band = pd.qcut(q['g'], 3, labels=False, duplicates='drop')
    if band.nunique() < 2:
        return None
    dim, bright = q[band == band.min()]['move'].mean(), q[band == band.max()]['move'].mean()
    return round(float(dim / bright), 2) if bright else None


def _level_at(df: pd.DataFrame, col: str, when) -> float | None:
    """Value of a level column in force at time `when` (last row at or before it)."""
    if df is None or df.empty or col not in df:
        return None
    prior = df[df['et'] <= when][col].dropna()
    if prior.empty:
        prior = df[col].dropna()
    return float(prior.iloc[-1]) if not prior.empty else None


def build_scorecard(day, sym: str = 'SPX', data_dir: str = _DATA_DIR) -> dict | None:
    import numpy as np
    ph_path = os.path.join(data_dir, f'price_history_{sym}.csv')
    snap_path = os.path.join(data_dir, f'gex_snapshots_{sym}_{day.isoformat()}.csv')
    if not (os.path.exists(ph_path) and os.path.exists(snap_path)):
        return None
    ph = pd.read_csv(ph_path)
    ph['dt'] = pd.to_datetime(ph['datetime'], unit='ms', utc=True).dt.tz_convert(ET)
    t = ph['dt'].dt.time
    px = ph[(ph['dt'].dt.date == day) & (t >= dtime(9, 30)) & (t <= dtime(15, 59))].reset_index(drop=True)
    if len(px) < 30:
        return None
    s = pd.read_csv(snap_path)
    s['et'] = s['timestamp'].map(_to_et)
    s = s[s['et'].map(lambda x: dtime(9, 30) <= x.time() <= dtime(16, 0))]
    if s.empty:
        return None
    p0 = os.path.join(data_dir, f'gex_0dte_snapshots_{sym}_{day.isoformat()}.csv')
    s0 = pd.read_csv(p0) if os.path.exists(p0) else None
    if s0 is not None and not s0.empty:
        s0['et'] = s0['timestamp'].map(_to_et)

    close, hi, lo = float(px['close'].iloc[-1]), float(px['high'].max()), float(px['low'].min())
    # Grade each wall with the level in force 15 min BEFORE price got closest to it: at
    # the moment of the high/low the rules may already have moved on (e.g. resist steps
    # aside within SR_MIN_PTS), which would grade the next wall instead of the one tested.
    from datetime import timedelta
    t_hi = px['dt'][px['high'].idxmax()] - timedelta(minutes=15)
    t_lo = px['dt'][px['low'].idxmin()] - timedelta(minutes=15)
    at = lambda h, m: datetime.combine(day, dtime(h, m), ET)
    row = {k: '' for k in SCORECARD_FIELDS}
    row.update(date=day.isoformat(), close=close, high=hi, low=lo,
               regime_close='POSITIVE' if s['total_gex'].iloc[-1] > 0 else 'NEGATIVE')

    # chain walls (ex-0DTE): the level in force when price got closest to it
    cw, pw = _level_at(s, 'call_wall', t_hi), _level_at(s, 'put_wall', t_lo)
    r = wall_reaction(px, cw, 'up')
    row.update(call_wall=cw, call_wall_touch=r['touch'], call_wall_break=r['break'])
    r = wall_reaction(px, pw, 'down')
    row.update(put_wall=pw, put_wall_touch=r['touch'], put_wall_break=r['break'])

    # gamma levels from the heatmap history (support / resist / trapdoor)
    L = current_gamma_levels(None, 'all', sym, data_dir, day=day)
    if L and L.get('history'):
        H = pd.DataFrame(L['history'])
        H['et'] = pd.to_datetime(H['time'], unit='s', utc=True).dt.tz_convert(ET)
        res, sup, trap = _level_at(H, 'resistance', t_hi), _level_at(H, 'support', t_lo), _level_at(H, 'trapdoor', t_lo)
        r = wall_reaction(px, res, 'up')
        row.update(resist=res, resist_touch=r['touch'], resist_break=r['break'], resist_gap_at_high=r['gap'])
        r = wall_reaction(px, sup, 'down')
        row.update(support=sup, support_touch=r['touch'], support_break=r['break'], support_gap_at_low=r['gap'])
        if trap is not None:
            row.update(trapdoor=trap, trapdoor_break=int(bool((px['close'] < trap).any())))

    # pins: distance from the close of the value in force at 14:00, 15:00 and 15:30. Not
    # the final value: by 16:00 0DTE gamma sits on the strike nearest price, so a final
    # pin lands near the close by construction and proves nothing.
    d = lambda v: round(abs(v - close), 2) if v is not None else ''
    row['pin_lt_dist'] = d(_level_at(s, 'pin', at(15, 59)))
    for hh, mm, tag in ((14, 0, '1400'), (15, 0, '1500'), (15, 30, '1530')):
        if 'true_pin' in s:
            row[f'true_pin_dist_{tag}'] = d(_level_at(s, 'true_pin', at(hh, mm)))
        if s0 is not None and not s0.empty:
            row[f'pin_0dte_dist_{tag}'] = d(_level_at(s0, 'pin', at(hh, mm)))

    # expected move from 14:00
    spot14 = _level_at(s, 'spot', at(14, 0))
    g14 = _level_at(s, 'total_gex', at(14, 0))
    if spot14 is not None and g14 is not None:
        table, _ = expected_move_table(data_dir)
        key = ('POSITIVE' if g14 > 0 else 'NEGATIVE', '14:00')
        mv = abs(close - spot14)
        row['em_1400_move'] = round(mv, 2)
        if key in table.index:
            med, p80 = float(table.loc[key, 'median']), float(table.loc[key, 'p80'])
            row.update(em_1400_median=round(med, 1), em_1400_p80=round(p80, 1),
                       em_1400_in_median=int(mv <= med), em_1400_in_p80=int(mv <= p80))

    # heatmap: is price slower where GEX at the price is higher?
    h = load_heatmap(sym, day=day, data_dir=data_dir)
    if h['times']:
        times, ks, cols = np.array(h['times']), np.array(h['strikes']), np.array(h['all'], float)
        tt = (px['datetime'] // 1000).to_numpy()
        idx = np.searchsorted(times, tt, side='right') - 1
        ok = idx >= 0
        g = np.full(len(px), np.nan)
        g[ok] = [np.interp(p, ks, cols[i]) for p, i in zip(px['close'].to_numpy()[ok], idx[ok])]
        # regime in force each minute (net GEX, all expiries, from the snapshots)
        st = (s['et'].map(lambda x: x.timestamp())).to_numpy()
        si = np.searchsorted(st, tt, side='right') - 1
        neg = np.where(si >= 0, s['total_gex'].to_numpy()[np.clip(si, 0, None)] < 0, False)
        q = pd.DataFrame({'g': g, 'move': px['close'].diff().abs(), 'hour': px['dt'].dt.hour,
                          'neg': neg}).dropna()
        if len(q):
            row['neg_share'] = round(float(q['neg'].mean()), 2)
        # The bright-band brake is a positive-gamma effect: on 2026-10-08 (negative
        # gamma, trend day) price ran through bright bands (0.93x). Graded per regime.
        for tag, part in (('pos', q[~q['neg']]), ('neg', q[q['neg']])):
            r = _speed_ratio(part)
            if r is not None:
                row[f'speed_ratio_{tag}'] = r
        if len(q) >= 60:
            q['band'] = pd.qcut(q['g'], 3, labels=False, duplicates='drop')
            dim, bright = q[q['band'] == q['band'].min()]['move'].mean(), q[q['band'] == q['band'].max()]['move'].mean()
            slower = hours = 0
            for _, hq in q.groupby('hour'):          # within each hour, so time of day doesn't drive it
                if len(hq) < 20:
                    continue
                med = hq['g'].median()
                hours += 1
                slower += int(hq[hq['g'] > med]['move'].mean() < hq[hq['g'] <= med]['move'].mean())
            row.update(speed_dim=round(dim, 3), speed_bright=round(bright, 3),
                       speed_ratio=round(dim / bright, 2) if bright else '',
                       speed_hours_slower=slower, speed_hours=hours)

    # charm flow: did its sign at 13:00 / 14:00 / 15:00 match the move into the close?
    # (recorded before CHARM_FIXED_FROM with the opposite sign: turned back here)
    if 'charm_flow' in s:
        hits = calls = 0
        flip = -1 if day < CHARM_FIXED_FROM else 1
        for hh in (13, 14, 15):
            cf, sp = _level_at(s, 'charm_flow', at(hh, 0)), _level_at(s, 'spot', at(hh, 0))
            if cf is None or sp is None or cf == 0 or abs(close - sp) < 2:
                continue
            calls += 1
            hits += int((flip * cf > 0) == (close > sp))
        if calls:
            row.update(charm_hits=hits, charm_calls=calls, charm_sign=CHARM_SIGN)
    return row


# The dealer charm flow had its sign backwards until the 2026-10-29 evening restart (bs.py
# charm is d(delta)/d(time passing); the flow treated it as per time remaining). Saved
# snapshots keep what was recorded; scorecard rows built since carry charm_sign=CHARM_SIGN.
CHARM_FIXED_FROM = date(2026, 10, 30)
CHARM_SIGN = 'dealer'


def fix_charm_scores(sym: str = 'SPX', data_dir: str = _DATA_DIR) -> int:
    """Scorecard rows scored on the backwards charm sign (no charm_sign): each counted call
    was a plain hit or miss, so the dealer sign's hits are calls - hits. Once per row (the
    mark), so it's safe to run every day. Returns the rows fixed."""
    path = os.path.join(data_dir, f'scorecard_{sym}.csv')
    if not os.path.exists(path):
        return 0
    df = pd.read_csv(path, dtype=str)
    if 'charm_sign' not in df:
        df['charm_sign'] = ''
    calls = pd.to_numeric(df['charm_calls'], errors='coerce')
    todo = calls.notna() & (calls > 0) & (df['charm_sign'].fillna('') != CHARM_SIGN)
    if not todo.any():
        return 0
    hits = pd.to_numeric(df['charm_hits'], errors='coerce').fillna(0)
    df.loc[todo, 'charm_hits'] = (calls[todo] - hits[todo]).astype(int).astype(str)
    df.loc[todo, 'charm_sign'] = CHARM_SIGN
    tmp = path + '.tmp'
    df.reindex(columns=SCORECARD_FIELDS).to_csv(tmp, index=False)
    os.replace(tmp, path)
    return int(todo.sum())


def write_scorecard(day, sym: str = 'SPX', data_dir: str = _DATA_DIR) -> dict | None:
    """Insert or replace the day's row in scorecard_<sym>.csv (sorted by date)."""
    row = build_scorecard(day, sym, data_dir)
    if not row:
        return None
    path = os.path.join(data_dir, f'scorecard_{sym}.csv')
    df = pd.read_csv(path, dtype=str) if os.path.exists(path) else pd.DataFrame(columns=SCORECARD_FIELDS)
    df = df[df['date'] != row['date']]
    df = pd.concat([df, pd.DataFrame([row]).astype(str)], ignore_index=True).sort_values('date')
    tmp = path + '.tmp'
    df.reindex(columns=SCORECARD_FIELDS).replace('None', '').to_csv(tmp, index=False)
    os.replace(tmp, path)
    return row


def scorecard_days_missing(sym: str = 'SPX', data_dir: str = _DATA_DIR) -> list:
    path = os.path.join(data_dir, f'scorecard_{sym}.csv')
    have = set(pd.read_csv(path, dtype=str)['date']) if os.path.exists(path) else set()
    today = datetime.now(ET).date()
    days = sorted({datetime.strptime(f[-14:-4], '%Y-%m-%d').date()
                   for f in glob.glob(os.path.join(data_dir, f'gex_snapshots_{sym}_2*.csv'))})
    return [d for d in days if d < today and d.isoformat() not in have]
