"""Δ FLOW rebuilt from option ticks: each trade (a rise in a contract's day volume between
two of its ticks) is classed against the quote standing before it, weighted by the
contract's delta, and summed per minute as the delta customers bought (= what dealers must
buy to stay hedged; call bought +, put bought -). Then tested against SPX like the old
once-a-minute version.

Usage: tick_flow_test.py            (all finished tick days; writes a per-minute cache)
"""
import glob
import math
import os
import statistics
import sys
import tempfile

import numpy as np
import pandas as pd

ROOT = r'C:\Productivity\TradingApp'
sys.path.insert(0, ROOT + r'\src')
os.chdir(ROOT)
import predictions as P  # noqa: E402

OUT = os.path.join(tempfile.gettempdir(), 'tick_flow_minutes.csv')   # per-minute cache; delete to rebuild
TODAY = pd.Timestamp.now(tz='America/New_York').date().isoformat()


def day_minutes(day: str) -> pd.DataFrame:
    """per minute: customer delta bought ($) by variant, plus classification counts"""
    t = pd.read_csv(f'data/option_ticks_SPX_{day}.csv.gz', dtype={'symbol': str})
    t = t.sort_values(['symbol', 'ts_ms'], kind='stable')
    g = t.groupby('symbol', sort=False)
    for c in ('volume', 'bid', 'ask', 'last'):
        t['p_' + c] = g[c].shift()
    t['dv'] = t['volume'] - t['p_volume']
    tr = t[(t['dv'] > 0) & t['p_volume'].notna()].copy()

    # the quote standing before the trade (Lee-Ready); a print at the mid goes by the tick test
    pb, pa, last = tr['p_bid'], tr['p_ask'], tr['last']
    ok = (pa > 0) & (pb >= 0) & (pa >= pb)
    mid = (pb + pa) / 2
    tick = np.sign(last - tr['p_last']).fillna(0)
    side = np.select([last >= pa, last <= pb, last > mid, last < mid], [1, -1, 1, -1], default=tick)
    tr['s'] = np.where(ok, side, 0)
    tr['at_quote'] = ok & ((last >= pa) | (last <= pb))
    spread = (pa - pb)
    # far outside the quote: usually one leg of a multi-leg order, priced as a package
    tr['outside'] = ok & ((last > pa + spread) | (last < pb - spread))
    # the quote didn't move on the trade's tick: no stale-quote bias either way
    tr['stable'] = ok & (tr['bid'] == pb) & (tr['ask'] == pa)
    # the same classification against the quote on the trade's own tick (after it)
    b2, a2 = tr['bid'], tr['ask']
    ok2 = (a2 > 0) & (b2 >= 0) & (a2 >= b2)
    m2 = (b2 + a2) / 2
    tr['s_after'] = np.where(ok2, np.select([last >= a2, last <= b2, last > m2, last < m2], [1, -1, 1, -1], default=tick), 0)

    sym = tr['symbol']
    tr['call'] = sym.str[12] == 'C'
    tr['strike'] = sym.str[13:].astype(int) / 1000
    tr['odte'] = ('20' + sym.str[6:8] + '-' + sym.str[8:10] + '-' + sym.str[10:12]) == day

    # each contract's delta and SPX from its minute bar (the one at or before the trade)
    bars = pd.read_csv(f'data/option_bars_SPX_{day}.csv.gz', usecols=['time', 'symbol', 'delta', 'gamma', 'spot'],
                       dtype={'symbol': str})
    bars['ts_ms'] = bars['time'] * 1000
    tr = tr.sort_values('ts_ms')
    tr = pd.merge_asof(tr, bars.sort_values('ts_ms')[['ts_ms', 'symbol', 'delta', 'gamma', 'spot']],
                       on='ts_ms', by='symbol', direction='backward')
    tr = tr.dropna(subset=['delta', 'spot'])
    unit = tr['dv'] * tr['delta'] * 100 * tr['spot']          # delta $ of the contracts traded (put delta < 0)
    tr['minute'] = (tr['ts_ms'] // 60000) * 60
    near = tr['delta'].abs().between(0.10, 0.90)
    v = {
        'all':        tr['s'] * unit,
        'all_after':  tr['s_after'] * unit,
        'clean':      tr['s'] * unit * ~tr['outside'],
        'odte':       tr['s'] * unit * tr['odte'],
        'odte_clean': tr['s'] * unit * tr['odte'] * ~tr['outside'],
        'odte_near':  tr['s'] * unit * tr['odte'] * ~tr['outside'] * near,
        'odte_quote': tr['s'] * unit * tr['odte'] * tr['at_quote'],   # only prints at / through the bid or ask
        'odte_stable': tr['s'] * unit * tr['odte'] * ~tr['outside'] * tr['stable'],
        'odte_stable_near': tr['s'] * unit * tr['odte'] * ~tr['outside'] * tr['stable'] * near,
        # gamma customers bought (dealers' gamma falls by as much): +, dealers get shorter gamma
        'odte_gamma': tr['s'] * tr['dv'] * tr['gamma'] * 100 * tr['spot'] ** 2 * 0.01 * tr['odte'],
    }
    out = pd.DataFrame({k: s.groupby(tr['minute']).sum() for k, s in v.items()})
    out['vol'] = tr.groupby('minute')['dv'].sum()
    out['vol_at_quote'] = tr['dv'].where(tr['at_quote'], 0).groupby(tr['minute']).sum()
    out['vol_unknown'] = tr['dv'].where(tr['s'] == 0, 0).groupby(tr['minute']).sum()
    out['vol_outside'] = tr['dv'].where(tr['outside'], 0).groupby(tr['minute']).sum()
    out['vol_stable'] = tr['dv'].where(tr['stable'], 0).groupby(tr['minute']).sum()
    out['day'] = day
    return out


def corr(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < 10 or x.std() == 0 or y.std() == 0:
        return float('nan')
    return float(np.corrcoef(x, y)[0, 1])


def main():
    days = sorted(os.path.basename(f)[17:27] for f in glob.glob('data/option_ticks_SPX_2026-*.csv.gz'))
    days = [d for d in days if d != TODAY]
    if os.path.exists(OUT):
        mins = pd.read_csv(OUT, index_col=0)
        days_have = set(mins['day'])
    else:
        mins, days_have = pd.DataFrame(), set()
    new = [day_minutes(d) for d in days if d not in days_have]
    if new:
        mins = pd.concat([mins, *new])
        mins.to_csv(OUT)
    bars = P.sessions('SPX')
    variants = ['all', 'all_after', 'clean', 'odte', 'odte_clean', 'odte_near', 'odte_quote', 'odte_gamma',
                'odte_stable', 'odte_stable_near']

    tot = mins[['vol', 'vol_at_quote', 'vol_unknown', 'vol_outside', 'vol_stable']].sum()
    print(f'{len(days)} days {days[0]} .. {days[-1]}; {int(tot.vol):,} contracts traded in the streamed set')
    print(f'  at / through the bid or ask {100 * tot.vol_at_quote / tot.vol:.0f}%, '
          f'unclassified {100 * tot.vol_unknown / tot.vol:.1f}%, far outside the quote {100 * tot.vol_outside / tot.vol:.1f}%, '
          f'quote unchanged on the trade tick {100 * tot.vol_stable / tot.vol:.0f}%')

    # per minute, aligned with SPX 1-min bars (bar t = [t, t+60))
    rows = []
    for d in days:
        b = bars.get(d)
        if not b:
            continue
        m = mins[mins['day'] == d]
        f = pd.DataFrame(b, columns=['t', 'o', 'h', 'l', 'c']).set_index('t')
        f = f.join(m[variants], how='left').fillna(0.0)
        f['ret'] = f['c'] - f['o']                     # the bar's own move
        f['day'] = d
        rows.append(f)
    df = pd.concat(rows)
    print(f'  {len(df):,} SPX minutes\n')

    def fwd(f, h):   # SPX from this bar's close to h bars later
        return f['c'].shift(-h) - f['c']

    def back(f, col, h):  # flow over this bar and the h-1 before it
        return f[col].rolling(h).sum()

    print('variant      same-min corr | flow last N min -> SPX next N min: corr, direction right (non-overlapping), n')
    for col in variants:
        same = corr(df[col], df['ret'])
        parts = [f'{col:<12} {same:+.2f}         |']
        for h in (5, 15, 30):
            xs, ys, hits = [], [], []
            for d, f in df.groupby('day'):
                fl, fu = back(f, col, h), fwd(f, h)
                ok = fl.notna() & fu.notna()
                xs += list(fl[ok]); ys += list(fu[ok])
                idx = [i for i in range(h - 1, len(f) - h, h)]
                for i in idx:
                    a, bb = fl.iloc[i], fu.iloc[i]
                    if a and bb:
                        hits.append((a > 0) == (bb > 0))
            n = len(hits)
            rate = 100 * sum(hits) / n
            ci = 196 * math.sqrt(0.25 / n)
            parts.append(f' {h}m: {corr(xs, ys):+.2f}, {rate:.0f}% ±{ci:.0f} (n {n})')
        print(''.join(parts))

    # baseline: the last N min of price itself
    base = []
    for h in (5, 15, 30):
        hits = []
        for d, f in df.groupby('day'):
            pb_, fu = f['c'] - f['c'].shift(h), fwd(f, h)
            for i in range(h, len(f) - h, h):
                a, bb = pb_.iloc[i], fu.iloc[i]
                if a and bb:
                    hits.append((a > 0) == (bb > 0))
        base.append(f'{h}m {100 * sum(hits) / len(hits):.0f}% (n {len(hits)})')
    print('price alone (last N min -> next N min): ' + ', '.join(base))

    # flow vs the move that already happened: does it just follow price?
    print('\nflow last N min vs SPX the SAME N min (does it follow price?):')
    for col in ('all', 'odte_clean', 'odte_quote', 'odte_stable'):
        out = []
        for h in (5, 15, 30):
            xs, ys = [], []
            for d, f in df.groupby('day'):
                fl, pm = back(f, col, h), f['c'] - f['c'].shift(h)
                ok = fl.notna() & pm.notna()
                xs += list(fl[ok]); ys += list(pm[ok])
            out.append(f'{h}m {corr(xs, ys):+.2f}')
        print(f'  {col:<12} ' + ', '.join(out))

    # the old once-a-minute series on the same days, for comparison
    import json
    old_same = []
    for d in days:
        p = f'data/delta_flow_{d}.json'
        if not os.path.exists(p) or d not in bars:
            continue
        j = json.load(open(p))
        pts = sorted((q['time'], q['value']) for q in j['0dte_call']) , sorted((q['time'], q['value']) for q in j['0dte_put'])
        f = df[df['day'] == d]
        def at(series, t):
            v = 0.0
            for tt, vv in series:
                if tt > t:
                    break
                v = vv
            return v
        net = np.array([at(pts[0], t + 59) + at(pts[1], t + 59) for t in f.index])
        old_same += list(zip(np.diff(net), f['ret'].values[1:], f['odte_clean'].values[1:]))
    if old_same:
        o, r, nw = zip(*old_same)
        print(f'\nsame days, same minute vs SPX: old 0DTE Δ FLOW corr {corr(o, r):+.2f}; tick version {corr(nw, r):+.2f}; '
              f'old vs tick corr {corr(o, nw):+.2f}')


if __name__ == '__main__':
    main()
