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
    path = os.path.join(data_dir, f'gex_grid_{sym}_{day.isoformat()}.jsonl')
    empty = {'times': [], 'strikes': [], 'all': [], 'odte': []}
    if not os.path.exists(path):
        return empty
    snaps = []
    with open(path, encoding='utf-8') as f:
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
    out = {'times': [], 'strikes': strikes, 'all': [], 'odte': []}
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
        out['all'].append([round(v) for v in all_col])
        out['odte'].append([round(v) for v in odte_col])
    return out
