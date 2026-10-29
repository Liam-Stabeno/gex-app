"""
Charm map backtest: at 13:00 / 14:00 / 15:00 / 15:30 ET on every day with a saved
gex_watchlist (0DTE delta + open interest per contract), the dealers' charm flow by price
(src/charm_map.py) and its PULL, the level the decay drifts price toward. Did it call the
move into the close?

  toward pull   SPX closed on the pull's side of the checkpoint price (|move| >= 2 pts)
  nearer        the close landed nearer the level than the checkpoint price was; the same
                for the 0DTE pin and TRUE PIN at that time (gex snapshots), as rivals
  error         median |close - level|; 'spot' = no move at all
  flow sign     the charm flow AT price (dealers buying: up), the header's number
Both with the 0DTE contracts only and with every watched contract.

Run:  python tools/charm_map_test.py      (the .venv or the system python)
"""
import glob
import math
import os
import statistics
import sys
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = r'C:\Productivity\TradingApp'
sys.path.insert(0, ROOT + r'\src')
os.chdir(ROOT)
import charm_map as cm  # noqa: E402
from gex_stats import day_file  # noqa: E402
import predictions as P  # noqa: E402

ET, PT = ZoneInfo('America/New_York'), 'America/Los_Angeles'
CHECKS = ((13, 0), (14, 0), (15, 0), (15, 30))
FRESH_SEC = 600            # a watchlist / gex snapshot older than this at a checkpoint: skip
MIN_MOVE = 2.0


def snap_times(df):
    return (pd.to_datetime(df['timestamp']).dt.tz_localize(PT, ambiguous='NaT', nonexistent='NaT')
            .dt.tz_convert(ET))


def asof(df, times, t):
    """the row of df last at or before t (and fresh), else None"""
    i = times.searchsorted(t, side='right') - 1
    if i < 0 or (t - times.iloc[i]).total_seconds() > FRESH_SEC:
        return None
    return df.iloc[i]


def day_rows(day: str, bars: list) -> list:
    p = day_file(f'gex_watchlist_SPX_{day}.csv', 'data')
    if not p or not bars:
        return []
    wl = pd.read_csv(p, dtype={'symbol': str})
    wl['et'] = snap_times(wl)
    wl = wl.dropna(subset=['et'])
    stamps = pd.Series(sorted(wl['et'].unique()))
    snaps = {}
    for name in ('gex_0dte_snapshots_SPX', 'gex_snapshots_SPX'):
        q = day_file(f'{name}_{day}.csv', 'data')
        if q:
            s = pd.read_csv(q)
            s['et'] = snap_times(s)
            s = s.dropna(subset=['et']).sort_values('et').reset_index(drop=True)
            snaps[name] = s
    times = [b[0] for b in bars]
    close = bars[-1][4]
    out = []
    for hh, mm in CHECKS:
        t = datetime.combine(date.fromisoformat(day), dtime(hh, mm), ET)
        i = max(j for j in range(len(times)) if times[j] <= t.timestamp()) if times[0] <= t.timestamp() else None
        if i is None:
            continue
        spot = bars[i][4]
        j = stamps.searchsorted(pd.Timestamp(t), side='right') - 1
        if j < 0 or (pd.Timestamp(t) - stamps.iloc[j]).total_seconds() > FRESH_SEC:
            continue
        rows = wl[wl['et'] == stamps.iloc[j]].to_dict('records')
        row = {'day': day, 'cp': f'{hh}:{mm:02d}', 'spot': spot, 'close': close}
        for tag, only0 in (('0dte', True), ('all', False)):
            c = cm.watchlist_contracts(rows, spot, t, zero_dte_only=only0)
            if not c:
                continue
            m = cm.charm_map(c, spot)
            row[f'pull_{tag}'] = m['pull'][0] if m['pull'] else None
            row[f'flow_{tag}'] = cm.flow_at(c, spot)
        for name, col, key in (('gex_0dte_snapshots_SPX', 'pin', 'pin0'), ('gex_snapshots_SPX', 'true_pin', 'true_pin'),
                               ('gex_0dte_snapshots_SPX', 'total_gex', 'gex_0dte'), ('gex_snapshots_SPX', 'total_gex', 'gex_all')):
            s = snaps.get(name)
            r = asof(s, s['et'], pd.Timestamp(t)) if s is not None else None
            if r is not None and col in r and pd.notna(r[col]):
                row[key] = float(r[col])
        out.append(row)
    return out


def rate(hits):
    hits = [h for h in hits if h is not None]
    if not hits:
        return '   -          '
    p = sum(hits) / len(hits)
    return f'{100 * p:4.0f}% ±{100 * 1.96 * math.sqrt(max(p * (1 - p), 1e-9) / len(hits)):3.0f} n{len(hits):<3d}'


def main():
    sessions = P.sessions('SPX', max_age=0)
    days = sorted({os.path.basename(f)[18:28] for f in glob.glob('data/gex_watchlist_SPX_2*.csv*')})
    rows = []
    for d in days:
        rows += day_rows(d, sessions.get(d, []))
    df = pd.DataFrame(rows)
    print(f'{df["day"].nunique()} days, {len(df)} checkpoints ({days[0]} .. {days[-1]})\n')

    def toward(r, lvl):
        mv, gap = r['close'] - r['spot'], (lvl - r['spot']) if lvl is not None and pd.notna(lvl) else None
        if gap is None or abs(mv) < MIN_MOVE or abs(gap) < 1:
            return None
        return (gap > 0) == (mv > 0)

    def nearer(r, lvl):
        if lvl is None or pd.isna(lvl):
            return None
        return abs(r['close'] - lvl) < abs(r['close'] - r['spot'])

    def flow_hit(r, f):
        if f is None or pd.isna(f) or f == 0 or abs(r['close'] - r['spot']) < MIN_MOVE:
            return None
        return (f > 0) == (r['close'] > r['spot'])

    print(f'{"checkpoint":10s} {"toward pull 0DTE":>17s} {"toward pull all":>17s} | {"nearer: pull 0DTE":>17s} {"0DTE pin":>17s} {"TRUE PIN":>17s} | {"flow sign 0DTE":>17s}')
    for cp in [c for c in ['13:00', '14:00', '15:00', '15:30'] if c in set(df['cp'])] + ['all']:
        sub = df if cp == 'all' else df[df['cp'] == cp]
        g = lambda col: sub[col] if col in sub else pd.Series([None] * len(sub), index=sub.index)
        print(f'{cp:10s} {rate([toward(r, r.get("pull_0dte")) for _, r in sub.iterrows()]):>17s} '
              f'{rate([toward(r, r.get("pull_all")) for _, r in sub.iterrows()]):>17s} | '
              f'{rate([nearer(r, r.get("pull_0dte")) for _, r in sub.iterrows()]):>17s} '
              f'{rate([nearer(r, r.get("pin0")) for _, r in sub.iterrows()]):>17s} '
              f'{rate([nearer(r, r.get("true_pin")) for _, r in sub.iterrows()]):>17s} | '
              f'{rate([flow_hit(r, r.get("flow_0dte")) for _, r in sub.iterrows()]):>17s}')

    print('\nmedian |close - level| (pts), checkpoints where the level exists')
    for cp in [c for c in ['13:00', '14:00', '15:00', '15:30'] if c in set(df['cp'])]:
        sub = df[df['cp'] == cp]
        parts = []
        for name, col in (('spot', 'spot'), ('pull 0DTE', 'pull_0dte'), ('pull all', 'pull_all'), ('0DTE pin', 'pin0'), ('TRUE PIN', 'true_pin')):
            if col not in sub:
                continue
            e = (sub['close'] - sub[col]).abs().dropna()
            parts.append(f'{name} {e.median():5.1f} (n{len(e)})')
        print(f'  {cp}  ' + '   '.join(parts))
    # by period and by the gamma regime at the checkpoint (dealers damping moves or adding to them)
    print('\nflow sign at price (0DTE) and toward pull, split')
    df['flow_hit'] = [flow_hit(r, r.get('flow_0dte')) for _, r in df.iterrows()]
    df['toward'] = [toward(r, r.get('pull_0dte')) for _, r in df.iterrows()]
    splits = [('Jun-Sep', df['day'] < '2026-10-01'), ('Oct on', df['day'] >= '2026-10-01')]
    for col, tag in (('gex_all', 'all expirations'), ('gex_0dte', '0DTE')):
        if col in df:
            splits += [(f'{tag} GEX +', df[col] > 0), (f'{tag} GEX -', df[col] < 0)]
    for name, mask in splits:
        sub = df[mask]
        print(f'  {name:22s} flow sign {rate(list(sub["flow_hit"]))}   toward pull {rate(list(sub["toward"]))}   '
              f'({sub["day"].nunique()} days)')
    have = df['pull_0dte'].notna().mean() if 'pull_0dte' in df else 0
    print(f'\na 0DTE pull within ±{cm.SPAN_PTS} pts at {100 * have:.0f}% of checkpoints')
    out = os.path.join(os.environ.get('TEMP', '.'), 'charm_map_checkpoints.csv')
    df.to_csv(out, index=False)
    print('rows:', out)


if __name__ == '__main__':
    main()
