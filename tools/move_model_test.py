"""
Are patterns emerging? Reads tools/build_move_panel.py's table and asks, day-forward (each 5
days scored by a model fitted only on the days before them), whether each block of recorded
data adds anything to price and ES volume alone, on the same rows:

  big     the next 5 / 15 / 30 / 60 min range is at least 1.5x usual: about to move
  up      it closes higher: which way
  low / high   the day's low / high prints in the next 5 / 30 min

Part 1 uses every session since ES volume began (ES only, hundreds of days): clock plus the
recent range against everything price and volume can give. Part 2 adds each recording block.
AUC 0.50 = no idea. 'edge' is the block's AUC over the base model's per test day, so one day
is one observation; a block counts only if its edge is above zero in both halves of the test
days and clearly so overall (|edge| > 2 x se).

Run:  python tools/move_model_test.py      (the system python: it has lightgbm + pandas)
"""
import os
import sys
from datetime import time as dtime

import numpy as np
import pandas as pd

ROOT = r'C:\Productivity\TradingApp'
DATA = os.path.join(ROOT, 'data')
PANEL = os.path.join(os.environ.get('TEMP', '.'), 'move_panel.csv')
HORIZONS = (5, 15, 30, 60)
FOLD_DAYS = 5
MIN_TRAIN_DAYS = 10
BASE = ['m', 'recent5', 'recent15', 'recent30', 'r5', 'r15', 'r30', 'pos', 'range_atr', 'to_hi', 'to_lo',
        'to_prev_close', 'to_prev_hi', 'to_prev_lo', 'es_vol5', 'es_vol5_30', 'es_dvol15']
TARGETS = [f'big_{h}' for h in HORIZONS] + [f'up_{h}' for h in HORIZONS] + \
          ['low_next_5', 'low_next_30', 'high_next_5', 'high_next_30']


def auc(y, s):
    y, s = np.asarray(y), np.asarray(s)
    r = pd.Series(s).rank().to_numpy()
    n1, n0 = int(y.sum()), int(len(y) - y.sum())
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else float('nan')


def fit_score(tr, te, target, features, big=False):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=150 if big else 80, learning_rate=0.03, num_leaves=7, min_child_samples=80 if big else 30,
                           subsample=0.8, subsample_freq=1, colsample_bytree=0.7, verbose=-1, random_state=1, n_jobs=4)
    m.fit(tr[features], tr[target])
    return m.predict_proba(te[features])[:, 1]


def walk(df, target, sets, min_train=MIN_TRAIN_DAYS, fold=FOLD_DAYS, window=None, big=False):
    """test rows with a column of scores per feature set"""
    d = df.dropna(subset=[target])
    days = sorted(d['day'].unique())
    parts = []
    for s in range(min_train, len(days), fold):
        tr = d[d['day'].isin(days[max(0, s - window) if window else 0:s])]
        te = d[d['day'].isin(days[s:s + fold])]
        if te.empty or tr[target].nunique() < 2:
            continue
        te = te.copy()
        for name, f in sets.items():
            te['p_' + name] = fit_score(tr, te, target, f, big)
        parts.append(te)
    return pd.concat(parts) if parts else pd.DataFrame()


def edge(t, target, a, b):
    """mean per-day AUC difference (a - b), its se, and the same for each half of the days"""
    g = [(day, auc(x[target], x['p_' + a]) - auc(x[target], x['p_' + b])) for day, x in t.groupby('day')
         if x[target].nunique() > 1 and x[target].sum() >= 1]
    g = [(k, v) for k, v in g if v == v]
    if len(g) < 6:
        return np.nan, np.nan, np.nan, np.nan, len(g)
    v = np.array([x[1] for x in g])
    h = len(v) // 2
    return v.mean(), v.std(ddof=1) / len(v) ** 0.5, v[:h].mean(), v[h:].mean(), len(v)


def es_panel():
    """the same price and volume inputs from ES bars alone, since 2024"""
    d = pd.read_csv(os.path.join(DATA, 'price_history_ES.csv'))
    d['dt'] = pd.to_datetime(d['datetime'], unit='ms', utc=True).dt.tz_convert('America/New_York')
    d = d[(d['dt'].dt.time >= dtime(9, 30)) & (d['dt'].dt.time < dtime(16, 0)) & (d['dt'].dt.weekday < 5)].copy()
    d['day'] = d['dt'].dt.date.astype(str)
    d['m'] = (d['dt'].dt.hour - 9) * 60 + d['dt'].dt.minute - 30
    grid, vol, cl, hi, lo, op = {}, {}, {}, {}, {}, {}
    for day, g in d.groupby('day'):
        if len(g) < 370:
            continue
        idx = g['m'].to_numpy(int).clip(0, 389)
        for store, col in ((vol, 'volume'), (cl, 'close'), (hi, 'high'), (lo, 'low'), (op, 'open')):
            a = np.full(390, np.nan)
            a[idx] = g[col].to_numpy(float)
            store[day] = a
    days = sorted(vol)
    H, L = np.array([hi[x] for x in days]), np.array([lo[x] for x in days])
    V = np.array([vol[x] for x in days])
    rows = []
    span = lambda A, B, n, m, k: np.nanmax(A[n, m:m + k]) - np.nanmin(B[n, m:m + k])
    for n in range(25, len(days)):
        prior = range(n - 20, n)
        atr = np.median([np.nanmax(H[q]) - np.nanmin(L[q]) for q in prior])
        c, h, l, v, o = cl[days[n]], H[n], L[n], V[n], op[days[n]]
        p_hi, p_lo, p_c = np.nanmax(H[n - 1]), np.nanmin(L[n - 1]), cl[days[n - 1]][~np.isnan(cl[days[n - 1]])][-1]
        d_lo, d_hi = int(np.nanargmin(l)), int(np.nanargmax(h))
        for m in range(30, 386, 5):
            px = o[m] if o[m] == o[m] else c[m - 1]
            if px != px:
                continue
            us = lambda mm, k: np.median([span(H, L, q, mm, k) for q in prior])
            r = dict(day=days[n], m=m)
            for k in (5, 15, 30):
                b = us(m - k, k)
                r[f'recent{k}'] = span(H, L, n, m - k, k) / b if b else np.nan
                r[f'r{k}'] = (px - c[m - k]) / atr
            sh, sl = np.nanmax(h[:m]), np.nanmin(l[:m])
            uv = np.median(np.nansum(V[n - 20:n, m - 5:m], axis=1))
            sg = np.nan_to_num((c[m - 15:m] - o[m - 15:m]) * v[m - 15:m])
            r.update(pos=(px - sl) / max(sh - sl, .25), range_atr=(sh - sl) / atr, to_hi=(sh - px) / atr, to_lo=(px - sl) / atr,
                     to_prev_close=(px - p_c) / atr, to_prev_hi=(px - p_hi) / atr, to_prev_lo=(px - p_lo) / atr,
                     es_vol5=np.nansum(v[m - 5:m]) / uv if uv else np.nan,
                     es_vol5_30=np.nanmean(v[m - 5:m]) / max(np.nanmean(v[m - 30:m]), 1.0),
                     es_dvol15=sg.sum() / max(np.abs(sg).sum(), 1.0))
            for hz in HORIZONS:
                if m + hz > 390:
                    continue
                b = us(m, hz)
                if b:
                    r[f'big_{hz}'] = int(span(H, L, n, m, hz) >= 1.5 * b)
                e = c[m + hz - 1]
                if e == e and e != px:
                    r[f'up_{hz}'] = int(e > px)
                r[f'low_next_{hz}'] = int(m <= d_lo < m + hz)
                r[f'high_next_{hz}'] = int(m <= d_hi < m + hz)
            rows.append(r)
    return pd.DataFrame(rows)


def part1():
    e = es_panel()
    print(f'== Part 1: ES since 2024: {len(e)} checkpoints, {e["day"].nunique()} days, {e["day"].min()} .. {e["day"].max()}')
    print(f'{"target":14s} {"base rate":>9s} {"clock":>7s} {"+range":>7s} {"all":>7s}   all vs clock+range, per day (later half)')
    sets = {'clock': ['m'], 'range': ['m', 'recent5', 'recent15', 'recent30'], 'all': BASE}
    for y in TARGETS:
        t = walk(e, y, sets, min_train=250, fold=40, window=250, big=True)
        if t.empty:
            continue
        mean, se, a, b, n = edge(t, y, 'all', 'range')
        print(f'{y:14s} {100 * t[y].mean():8.1f}% {auc(t[y], t["p_clock"]):7.3f} {auc(t[y], t["p_range"]):7.3f} {auc(t[y], t["p_all"]):7.3f}'
              f'   {mean:+.3f} ±{2 * se:.3f} ({a:+.3f} / {b:+.3f}) {n}d')
        if y in ('big_5', 'big_15', 'big_30'):
            t = t.assign(q=pd.qcut(t['p_all'], 10, labels=False, duplicates='drop'))
            top, rest = t[t['q'] == t['q'].max()], t[t['q'] != t['q'].max()]
            print(f'    most confident 10%: {100 * top[y].mean():.0f}% big moves vs {100 * rest[y].mean():.0f}% the rest (n {len(top)})')
    return e


def part2():
    d = pd.read_csv(PANEL)
    blocks = {
        'gex': ['gex_net', 'd_put_wall', 'd_call_wall', 'd_pin', 'd_true_pin', 'd_flip_level', 'near_level'],
        'grid': ['g_at', 'g_up', 'g_dn', 'd_gold', 'd_support', 'd_resistance', 'd_trapdoor', 'd_squeeze'],
        'charm': [c for c in d.columns if c.startswith('ch_')],
        'flow': ['fl_imb15', 'fl_imb5'],
        'book': ['bk_imb', 'bk_spread', 'bk_large'],
        'internals': [c for c in d.columns if c.startswith('in_')],
    }
    print(f'\n== Part 2: {len(d)} checkpoints, {d["day"].nunique()} SPX days; does each recording add to price + ES volume?')
    tests = wins = 0
    found = []
    for name, cols in blocks.items():
        have = d[d[cols].notna().any(axis=1)]
        days = have['day'].nunique()
        print(f'\n{name}: {days} days, {len(have)} rows; {len(cols)} inputs')
        if days < MIN_TRAIN_DAYS + 3:
            print('  too few days')
            continue
        for y in TARGETS:
            t = walk(have, y, {'base': BASE, 'with': BASE + cols})
            if t.empty or t[y].nunique() < 2:
                continue
            mean, se, a, b, n = edge(t, y, 'with', 'base')
            if mean != mean:
                continue
            tests += 1
            good = abs(mean) > 2 * se and mean > 0 and a > 0 and b > 0
            wins += good
            print(f'  {y:13s} base {auc(t[y], t["p_base"]):.3f} with {auc(t[y], t["p_with"]):.3f}  edge {mean:+.3f} ±{2 * se:.3f} '
                  f'({a:+.3f} / {b:+.3f}) {n}d {"<- adds" if good else ""}')
            if good:
                found.append((name, y, round(mean, 3), round(se, 3), n))
    print(f'\n{tests} tests; about {tests * 0.01:.1f} would pass by chance; passed:', found or 'none')


def part3(e):
    """the honest versions of two scores above. The day's low can only print when price is near
    the low so far, which makes low_next look easy: here only the moments near an extreme
    count. And volatility stays volatile, which makes big look easy: here only calm moments."""
    print("\n== Part 3a: price already within 5% of a usual day's range of the session low / high: is this it?")
    sets = {'clock': ['m'], 'all': BASE}
    for side, near in (('low', 'to_lo'), ('high', 'to_hi')):
        d = e[e[near] <= 0.05]
        for h in (5, 30):
            y = f'{side}_next_{h}'
            t = walk(d, y, sets, min_train=150, fold=40, window=250, big=True)
            q = t.assign(q=pd.qcut(t['p_all'], 10, labels=False, duplicates='drop'))
            top = q[q['q'] == q['q'].max()]
            days = sorted(t['day'].unique())
            late = t[t['day'] >= days[len(days) // 2]]
            print(f'  {y:13s} {len(t)} moments, base rate {100 * t[y].mean():4.1f}%  AUC all {auc(t[y], t["p_all"]):.3f} clock {auc(t[y], t["p_clock"]):.3f}'
                  f'  later half {auc(late[y], late["p_all"]):.3f} / {auc(late[y], late["p_clock"]):.3f}'
                  f'  most confident 10%: {100 * top[y].mean():.0f}% right vs clock says {100 * top["p_clock"].mean():.0f}%')
    print('\n== Part 3b: calm moments only (last 5 and 15 min under usual): does a move start?')
    d = e[(e['recent5'] < 1) & (e['recent15'] < 1)]
    sets = {'clock': ['m'], 'range': ['m', 'recent5', 'recent15', 'recent30'], 'all': BASE}
    for h in HORIZONS:
        y = f'big_{h}'
        t = walk(d, y, sets, min_train=150, fold=40, window=250, big=True)
        mean, se, a, b, n = edge(t, y, 'all', 'range')
        q = t.assign(q=pd.qcut(t['p_all'], 10, labels=False, duplicates='drop'))
        top = q[q['q'] == q['q'].max()]
        print(f'  {y:7s} {len(t)} moments, base {100 * t[y].mean():4.1f}%  AUC clock {auc(t[y], t["p_clock"]):.3f} range {auc(t[y], t["p_range"]):.3f} all {auc(t[y], t["p_all"]):.3f}'
              f'  edge {mean:+.3f} ±{2 * se:.3f} ({a:+.3f} / {b:+.3f})  most confident 10%: {100 * top[y].mean():.0f}%')


def main():
    cache = os.path.join(os.environ.get('TEMP', '.'), 'es_panel.pkl')
    if 'part3' in sys.argv and os.path.exists(cache):
        part3(pd.read_pickle(cache))
        return
    e = part1()
    e.to_pickle(cache)
    part3(e)
    part2()


if __name__ == '__main__':
    main()
