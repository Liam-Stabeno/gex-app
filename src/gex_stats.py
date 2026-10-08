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
from datetime import datetime, time as dtime
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


def load_heatmap(sym: str = 'SPX', day=None, band: float = 250.0,
                 data_dir: str = _DATA_DIR) -> dict:
    """Net GEX per strike over time, from gex_grid_<sym>_<date>.jsonl (5-min snapshots).

    Returns {'times': [epoch s], 'strikes': [...], 'all': [[gex per strike] per time],
             'odte': [...]} for strikes within `band` pts of the day's spot range.
    Positive = dealers long gamma (hedging dampens moves); negative = amplifies.
    """
    import json
    day = day or datetime.now(ET).date()
    path = day_file(f'gex_grid_{sym}_{day.isoformat()}.jsonl', data_dir)
    empty = {'times': [], 'spots': [], 'strikes': [], 'all': [], 'odte': []}
    if not path:
        return empty
    snaps = []
    with open_text(path) as f:
        for line in f:
            try:
                snaps.append(json.loads(line))
            except ValueError:
                continue
    if not snaps:
        return empty
    spots = [s['spot'] for s in snaps if s.get('spot')]
    lo, hi = min(spots) - band, max(spots) + band
    strikes = sorted({r[0] for s in snaps for r in s['rows'] if lo <= r[0] <= hi})
    idx = {k: i for i, k in enumerate(strikes)}
    out = {'times': [], 'spots': [], 'strikes': strikes, 'all': [], 'odte': []}
    for s in snaps:
        today_i = s['exp'].index(day.isoformat()) if day.isoformat() in s['exp'] else -1
        all_col, odte_col = [0.0] * len(strikes), [0.0] * len(strikes)
        for k, exp_i, _c, _p, gex in s['rows']:
            i = idx.get(k)
            if i is None:
                continue
            all_col[i] += gex
            if exp_i == today_i:
                odte_col[i] += gex
        out['times'].append(int(datetime.fromisoformat(s['ts']).timestamp()))
        out['spots'].append(s.get('spot'))
        out['all'].append([round(v) for v in all_col])
        out['odte'].append([round(v) for v in odte_col])
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


def current_gamma_levels(spot: float | None, mode: str = 'all', sym: str = 'SPX',
                         data_dir: str = _DATA_DIR, day=None) -> dict | None:
    """gamma_levels() on the latest 5-min snapshot of a day's heatmap (default today).
    For a past day, spot defaults to that day's last snapshot."""
    h = load_heatmap(sym, day=day, data_dir=data_dir)
    if not h['times'] or mode not in ('all', 'odte'):
        return None
    if spot is None:
        spot = next((s for s in reversed(h['spots']) if s), None)
        if spot is None:
            return None
    # the same levels for every 5-min snapshot of the day, in order, each one sticky
    # to the one before — so the chart can trail them
    history, prev = [], None
    for t, col, sp in zip(h['times'], h[mode], h['spots']):
        if not sp:
            continue
        L = gamma_levels(h['strikes'], col, float(sp), prev=prev)
        prev = {k: (L[k]['strike'] if L.get(k) else None) for k in ('support', 'resistance', 'trapdoor', 'squeeze')}
        history.append({'time': t, **prev})
    out = gamma_levels(h['strikes'], h[mode][-1], spot, prev=prev)
    if out:
        out['history'] = history
        # air pockets are about total gamma: 0DTE alone looks empty away from spot
        # even where other expiries hold plenty
        if mode != 'all':
            out['air_pockets'] = gamma_levels(h['strikes'], h['all'][-1], spot).get('air_pockets', [])
        out.update(mode=mode, as_of=h['times'][-1])
    return out


# ── Long-term storage ──────────────────────────────────────────────────────────
# Day files are kept forever. Once a day is over, the large ones are gzipped
# (~10x smaller); readers accept either form. A one-row-per-day summary CSV
# (daily_summary_<sym>.csv) is the quick way to track things day to day.
ARCHIVE_PATTERNS = ('gex_grid_{sym}_*.jsonl', 'rolling_profile_{sym}_*.jsonl', 'gex_watchlist_{sym}_*.csv')


def day_file(name: str, data_dir: str = _DATA_DIR) -> str | None:
    """Path of a day file, plain or gzipped (None if neither exists)."""
    plain = os.path.join(data_dir, name)
    for p in (plain, plain + '.gz'):
        if os.path.exists(p):
            return p
    return None


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
    'pin_lt_dist', 'true_pin_dist', 'pin_0dte_dist', 'true_pin_dist_1400', 'pin_0dte_dist_1400',
    'em_1400_move', 'em_1400_median', 'em_1400_p80', 'em_1400_in_median', 'em_1400_in_p80',
    'speed_dim', 'speed_bright', 'speed_ratio', 'speed_hours_slower', 'speed_hours',
    'charm_hits', 'charm_calls',
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

    # pins: distance from the close (final value, and the value in force at 14:00)
    d = lambda v: round(abs(v - close), 2) if v is not None else ''
    row.update(pin_lt_dist=d(_level_at(s, 'pin', at(15, 59))),
               true_pin_dist=d(_level_at(s, 'true_pin', at(15, 59))) if 'true_pin' in s else '',
               true_pin_dist_1400=d(_level_at(s, 'true_pin', at(14, 0))) if 'true_pin' in s else '')
    if s0 is not None and not s0.empty:
        row.update(pin_0dte_dist=d(_level_at(s0, 'pin', at(15, 59))),
                   pin_0dte_dist_1400=d(_level_at(s0, 'pin', at(14, 0))))

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
        q = pd.DataFrame({'g': g, 'move': px['close'].diff().abs(), 'hour': px['dt'].dt.hour}).dropna()
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
    if 'charm_flow' in s:
        hits = calls = 0
        for hh in (13, 14, 15):
            cf, sp = _level_at(s, 'charm_flow', at(hh, 0)), _level_at(s, 'spot', at(hh, 0))
            if cf is None or sp is None or cf == 0 or abs(close - sp) < 2:
                continue
            calls += 1
            hits += int((cf > 0) == (close > sp))
        if calls:
            row.update(charm_hits=hits, charm_calls=calls)
    return row


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
