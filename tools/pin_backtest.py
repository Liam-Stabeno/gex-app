"""
Backtest pin-style levels against the SPX 16:00 close.

    python tools/pin_backtest.py              # full report
    python tools/pin_backtest.py --weights    # also re-fit TRUE PIN weights (slower)

Uses the history the dashboard saves under data/:
    gex_snapshots_SPX_<date>.csv        levels per refresh (ex-0DTE chain), spot, total GEX
    gex_0dte_snapshots_SPX_<date>.csv   0DTE levels per refresh
    gex_watchlist_SPX_<date>.csv        delta + OI per watched contract per refresh
    price_history_SPX.csv               1-min candles (16:00 close)

TRUE PIN is rebuilt from the watchlist: vol is backed out of each strike's OTM
delta (unique root), then gamma/charm/vanna come from Black-Scholes, as in the
live loop. Dealer positioning uses the dashboard's convention: long calls (+),
short puts (-).

Report sections
  1. Median distance from each level to the close, by checkpoint time
  2. When a level was >= 10 pts from spot, how often the close moved toward it
  3. Expected move to the close by GEX regime (what the dashboard shows)
  4. Direction test: does the sign of the dealer charm (and vanna) flow
     predict the direction of the move into the close?
  5. (--weights) TRUE PIN weight search, in-sample and leave-one-day-out

Small samples: checkpoints within a day are correlated, and many candidates are
compared, so treat edges under ~60% or from < 50 cases as unproven.
"""
import argparse
import itertools
import math
import os
import sys
from datetime import datetime, time as dtime
from statistics import NormalDist

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
import bs                                    # noqa: E402
from gex_stats import ET, load_closes, load_checkpoints, expected_move_table  # noqa: E402
from gex import TRUE_PIN_WEIGHTS             # noqa: E402

DATA = os.path.join(ROOT, 'data')
R = 0.05
_N = NormalDist()


def _expiry(sym: str):
    return datetime.strptime(sym.split()[-1][:6], '%y%m%d').date()


def _sigma_from_delta(delta, side, S, K, T):
    """Vol from an OTM delta: d1 = N^-1(delta), then the unique positive root."""
    p = delta if side == 'call' else delta + 1.0
    if not (0.01 < p < 0.99):
        return None
    d1 = _N.inv_cdf(p)
    a, b, c = T / 2.0, -d1 * math.sqrt(T), math.log(S / K) + R * T
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    pos = [r for r in ((-b + s * math.sqrt(disc)) / (2 * a) for s in (1, -1)) if r > 0]
    if not pos:
        return None
    v = min(pos) if c > 0 else max(pos)
    return v if 0.01 < v < 3.0 else None


def rebuild_greeks(wl: pd.DataFrame, spot: float, ts: datetime) -> pd.DataFrame:
    """Per-contract GEX and charm/vanna exposures from saved delta + OI."""
    wl = wl.assign(exp=wl['symbol'].map(_expiry))
    rows = []
    for (exp, K), g in wl.groupby(['exp', 'strike']):
        close = datetime.combine(exp, dtime(16, 0), ET)
        if close < ts:
            continue
        T = max((close - ts).total_seconds(), 300) / (365 * 24 * 3600)
        otm = 'call' if K >= spot else 'put'
        sig = None
        for side in (otm, 'put' if otm == 'call' else 'call'):
            r = g[g['side'] == side]
            if not r.empty and (sig := _sigma_from_delta(float(r['delta'].iloc[0]), side, spot, K, T)):
                break
        if not sig:
            continue
        gam, cha, van = bs.greeks(spot, K, T, R, sig)
        for _, r in g.iterrows():
            sign, oi = (1 if r['side'] == 'call' else -1), float(r['oi'])
            scale = oi * 100 * spot
            rows.append((K, exp == ts.date(), r['side'], oi, gam * scale * sign,
                         abs(cha) * scale, abs(van) * scale, cha * scale * sign, van * scale * sign))
    return pd.DataFrame(rows, columns=['K', 'is0', 'side', 'oi', 'gex', 'charm_abs',
                                       'vanna_abs', 'charm_s', 'vanna_s'])


def _true_pin(by: pd.DataFrame, w: dict) -> float:
    norm = lambda s: s / (s.sum() or 1.0)
    score = w['gamma'] * norm(by['gex'].abs()) + w['charm'] * norm(by['charm']) + w['vanna'] * norm(by['vanna'])
    return float(score.idxmax())


def collect():
    closes = load_closes(DATA)
    cps = load_checkpoints(data_dir=DATA)
    cps = cps[cps['day'].isin(closes)]
    cand_rows, comp_rows, flow_rows = [], [], []
    cache = {}
    for _, cp in cps.iterrows():
        day = cp['day'].isoformat()
        if day not in cache:
            p0 = os.path.join(DATA, f'gex_0dte_snapshots_SPX_{day}.csv')
            pw = os.path.join(DATA, f'gex_watchlist_SPX_{day}.csv')
            cache = {day: (pd.read_csv(p0) if os.path.exists(p0) else None,
                           pd.read_csv(pw) if os.path.exists(pw) else None)}
        s0, wl = cache[day]
        spot, close, ts = float(cp['spot']), closes[cp['day']], cp['et']
        c = {'spot (no move)': spot, 'nearest 25': round(spot / 25) * 25,
             'nearest 50': round(spot / 50) * 50, 'Pin (ex-0DTE)': cp.get('pin'),
             'Call wall (ex-0DTE)': cp.get('call_wall'), 'Flip (ex-0DTE)': cp.get('flip_level')}
        if s0 is not None and (s0['timestamp'] == cp['timestamp']).any():
            c['Pin (0DTE)'] = s0[s0['timestamp'] == cp['timestamp']].iloc[0].get('pin')
        if wl is not None:
            d = rebuild_greeks(wl[wl['timestamp'] == cp['timestamp']], spot, ts)
            if not d.empty:
                by = d.groupby('K').agg(gex=('gex', 'sum'), charm=('charm_abs', 'sum'), vanna=('vanna_abs', 'sum'))
                c['True Pin (old 40/35/25)'] = _true_pin(by, {'gamma': .40, 'charm': .35, 'vanna': .25})
                c[f"True Pin (current {TRUE_PIN_WEIGHTS['gamma']:.0%} gamma)"] = _true_pin(by, TRUE_PIN_WEIGHTS)
                z = d[d['is0']]
                if not z.empty:
                    calls = z[z['side'] == 'call'].groupby('K')['oi'].sum()
                    puts = z[z['side'] == 'put'].groupby('K')['oi'].sum()
                    ks = np.array(sorted(set(calls.index) | set(puts.index)))
                    pain = [(np.maximum(x - calls.index.values, 0) * calls.values).sum()
                            + (np.maximum(puts.index.values - x, 0) * puts.values).sum() for x in ks]
                    c['0DTE max pain'] = float(ks[int(np.argmin(pain))])
                comp_rows.append({'day': day, 'close': close, 'by': by})
                flow_rows.append({'day': day, 'cp': cp['cp'], 'move': close - spot,
                                  'charm_flow': d['charm_s'].sum(), 'vanna_flow': d['vanna_s'].sum(),
                                  'charm_flow_0dte': d.loc[d['is0'], 'charm_s'].sum(),
                                  'regime': 'POSITIVE' if cp['total_gex'] > 0 else 'NEGATIVE'})
        for name, lvl in c.items():
            if lvl is None or (isinstance(lvl, float) and math.isnan(lvl)):
                continue
            cand_rows.append({'day': day, 'cp': cp['cp'], 'cand': name, 'level': float(lvl),
                              'spot': spot, 'close': close, 'err': abs(float(lvl) - close),
                              'regime': 'POSITIVE' if cp['total_gex'] > 0 else 'NEGATIVE'})
    return pd.DataFrame(cand_rows), comp_rows, pd.DataFrame(flow_rows)


def report(res, comps, flows, fit_weights):
    print(f"Days: {res['day'].nunique()}   checkpoints: {len(res.groupby(['day', 'cp']))}   "
          f"({res['day'].min()} to {res['day'].max()})\n")

    print('1. Median distance to the 16:00 close (pts) by checkpoint ET — lower is better')
    piv = res.pivot_table(index='cand', columns='cp', values='err', aggfunc='median')
    piv['ALL'] = res.groupby('cand')['err'].median()
    piv['<=10pt'] = res.groupby('cand')['err'].apply(lambda e: (e <= 10).mean())
    print(piv.sort_values('ALL').round(1).to_string(), '\n')

    print('2. Level >= 10 pts from spot: share of closes that moved toward it (50% = coin flip)')
    far = res[(res['level'] - res['spot']).abs().ge(10) & (res['cand'] != 'spot (no move)')].copy()
    far['toward'] = np.sign(far['close'] - far['spot']) == np.sign(far['level'] - far['spot'])
    t = far.groupby('cand')['toward'].agg(['mean', 'size'])
    for rg in ('POSITIVE', 'NEGATIVE'):
        g = far[far['regime'] == rg].groupby('cand')['toward']
        t[f'{rg[:3]} %'], t[f'{rg[:3]} n'] = g.mean(), g.size()
    print(t.sort_values('mean', ascending=False).round(2).to_string(), '\n')

    print('3. Expected move to the close by GEX regime (pts) — shown on the dashboard')
    table, days = expected_move_table(DATA)
    print(table.round(1).unstack(0).to_string(), f'\n   from {days} days\n')

    print('4. Direction: does the sign of the dealer flow predict the move into the close?')
    print('   (dealers long calls / short puts; positive flow = dealers buying)')
    for col in ('charm_flow', 'charm_flow_0dte', 'vanna_flow'):
        f = flows[(flows[col] != 0) & (flows['move'].abs() >= 2)]
        hit = (np.sign(f[col]) == np.sign(f['move']))
        by_rg = {rg: f'{hit[f["regime"] == rg].mean():.0%} (n={int((f["regime"] == rg).sum())})'
                 for rg in ('POSITIVE', 'NEGATIVE')}
        late = f['cp'] >= '14:00'
        print(f'   {col:16s} all {hit.mean():.0%} (n={len(f)})  from 14:00 {hit[late].mean():.0%} '
              f'(n={int(late.sum())})  by regime {by_rg}')
    print()

    if fit_weights:
        print('5. TRUE PIN weights (gamma, charm, vanna): median distance to close')
        grid = [w for w in itertools.product(np.arange(0, 1.01, 0.1), repeat=3) if abs(sum(w) - 1) < 1e-9]
        err = lambda rows, w: [abs(_true_pin(x['by'], dict(zip(('gamma', 'charm', 'vanna'), w))) - x['close'])
                               for x in rows]
        full = sorted(((w, np.median(err(comps, w))) for w in grid), key=lambda kv: kv[1])
        for w, e in full[:5]:
            print(f'   {tuple(round(x, 1) for x in w)}  {e:.1f}')
        cur = tuple(TRUE_PIN_WEIGHTS[k] for k in ('gamma', 'charm', 'vanna'))
        print(f'   current {cur}: {np.median(err(comps, cur)):.1f}')
        oos = []
        for d in sorted({x['day'] for x in comps}):
            train = [x for x in comps if x['day'] != d]
            w = min(grid, key=lambda w: np.median(err(train, w)))
            oos += err([x for x in comps if x['day'] == d], w)
        print(f'   leave-one-day-out: {np.median(oos):.1f} over {len(oos)} checkpoints')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--weights', action='store_true', help='also re-fit TRUE PIN weights')
    a = ap.parse_args()
    report(*collect(), fit_weights=a.weights)
