"""
How far, and which way, does SPX go when it reaches one of our levels? Every approach (price
at least ARM_PTS away, then within TOUCH_PTS, one per approach) on every saved day, measured
from the CLOSE of the bar that touched, the first moment anyone could act:

  through    pts on through the level's side at +5 / +15 / +30 / +60 min (negative = bounced)
  reach      the most it went each way within 60 min
  first      which came first, MOVE_PTS through or MOVE_PTS back

A level matters only if it beats what any price does. So each level is run again at fake
prices (the same level shifted by a random amount, a few times a day), and round 25s are run
as a plain rival. 'edge' is the level's mean through move minus its fakes', with a 95% error
bar over days. Then the same split by what else was known: GEX at the strike, net GEX, the
clock, ES volume, the charm flow's direction, how fast price came in.

Run:  python tools/level_move_test.py      (the .venv) -> %TEMP%\\level_moves.csv
"""
import math
import os
import random
import statistics
import sys
from bisect import bisect_right
from datetime import date

import numpy as np
import pandas as pd

ROOT = r'C:\Productivity\TradingApp'
sys.path.insert(0, ROOT + r'\src')
os.chdir(ROOT)
import charm_map as cm  # noqa: E402
import gex_stats  # noqa: E402
import predictions as P  # noqa: E402
from datetime import datetime  # noqa: E402

ARM_PTS, TOUCH_PTS, MOVE_PTS = 8.0, 2.0, 5.0
HORIZONS = (5, 15, 30, 60)
FRESH_SEC = 300
FAKES = 3
SNAP = ('put_wall', 'call_wall', 'pin', 'true_pin', 'flip_level')


def levels_by_bar(day, bars, by_day, days):
    """[{name: price}] for each bar, as known at its open"""
    snaps = P.snapshots(day)
    snaps0 = P.snapshots(day, zero_dte=True)
    st, st0 = [x[0] for x in snaps], [x[0] for x in snaps0]
    heat = gex_stats.load_heatmap('SPX', day=date.fromisoformat(day))
    tl = P.level_timeline(heat) if heat.get('times') else []
    tt = [x[0] for x in tl]
    n = days.index(day)
    prev = by_day[days[n - 1]] if n else None
    out, gex_at, net = [], [], []
    for i, b in enumerate(bars):
        t = b[0]
        lv = {}
        j = bisect_right(st, t) - 1
        g = None
        if j >= 0 and t - st[j] <= FRESH_SEC:
            r = snaps[j][1]
            for k in SNAP:
                if r.get(k) not in (None, ''):
                    lv[k] = float(r[k])
            g = float(r['total_gex']) if r.get('total_gex') not in (None, '') else None
        j = bisect_right(st0, t) - 1
        if j >= 0 and t - st0[j] <= FRESH_SEC:
            r = snaps0[j][1]
            for k in ('put_wall', 'call_wall'):
                if r.get(k) not in (None, ''):
                    lv['0dte_' + k] = float(r[k])
        by_k = {}
        j = bisect_right(tt, t) - 1
        if j >= 0 and t - tt[j] <= FRESH_SEC:
            names, by_k = tl[j][1], tl[j][2]
            for k, ns in names.items():
                for nm in ns:
                    lv.setdefault(nm, k) if nm != 'wall' else lv.setdefault(f'wall_{k}', k)
            if by_k:
                lv['gold'] = max(by_k, key=lambda x: abs(by_k[x]))
        if i >= 5:
            lv['open'] = bars[0][1]
        if prev:
            lv.update(prev_close=prev[-1][4], prev_open=prev[0][1], prev_high=max(x[2] for x in prev), prev_low=min(x[3] for x in prev))
        out.append(lv)
        gex_at.append(by_k)
        net.append(g)
    return out, gex_at, net


def family(name):
    return 'gex wall' if name.startswith('wall_') else name


def scan(day, bars, lv_rows, gex_at, net, usual, line_t, lines, es_vol, rng):
    closes = [b[4] for b in bars]
    rows = []
    shifts = {}
    armed = {}

    def touch(i, tag, fam, k, real):
        side = armed.get((tag, k))
        pc = closes[i - 1]
        hi, lo = bars[i][2], bars[i][3]
        if not side:
            if abs(pc - k) >= ARM_PTS:
                armed[(tag, k)] = side = 1 if pc > k else -1
            else:
                return
        if not (lo <= k + TOUCH_PTS if side > 0 else hi >= k - TOUCH_PTS):
            return
        armed[(tag, k)] = 0
        d = -side                                          # the way through
        c = closes[i]
        rec = dict(day=day, m=(bars[i][0] - bars[0][0]) // 60, level=fam, real=real, price=k, frm='above' if side > 0 else 'below', entry=c)
        for h in HORIZONS:
            rec[f'thru_{h}'] = d * (closes[i + h] - c) if i + h < len(bars) else np.nan
            rec[f'abs_{h}'] = abs(closes[i + h] - c) if i + h < len(bars) else np.nan
        w = bars[i + 1:i + 61]
        if w:
            rec['reach_thru'] = max(0.0, max(d * (x[2 if d > 0 else 3] - c) for x in w))
            rec['reach_back'] = max(0.0, max(-d * (x[3 if d > 0 else 2] - c) for x in w))
        first = None
        for x in w:
            a = d * (x[2 if d > 0 else 3] - c) >= MOVE_PTS
            b = -d * (x[3 if d > 0 else 2] - c) >= MOVE_PTS
            if a and b:
                break
            if a or b:
                first = 'through' if a else 'back'
                break
        rec['first'] = first
        g_k = gex_at[i].get(k)
        rec['gex_strike'] = None if g_k is None or not real else ('pos' if g_k > 0 else 'neg')
        rec['net'] = None if net[i] is None else ('pos' if net[i] > 0 else 'neg')
        rec['speed'] = abs(c - closes[max(i - 15, 0)])
        rec['es_vol'] = es_vol(bars[i][0])
        j = bisect_right(line_t, bars[i][0]) - 1
        if j >= 0 and bars[i][0] - line_t[j] <= FRESH_SEC:
            f = lines[j]['flow']
            rec['charm'] = 'with' if (f[len(f) // 2] > 0) == (d > 0) else 'against'
        rows.append(rec)

    for i in range(5, len(bars)):
        lv = lv_rows[i]
        for name, k in lv.items():
            fam = family(name)
            touch(i, 'r' + name, fam, k, True)
            s = shifts.setdefault(fam, [rng.choice((-1, 1)) * rng.uniform(11, 39) for _ in range(FAKES)])
            for n, sh in enumerate(s):
                touch(i, f'f{n}{name}', fam, k + sh, False)
        c = closes[i - 1]
        r25 = 25 * round(c / 25)
        for q in (r25 - 25, r25, r25 + 25):
            touch(i, 'r25', 'round 25', q, True)
    return rows


def ci(df, col):
    g = df.groupby('day')[col].mean().dropna()
    return g.mean(), 1.96 * g.std(ddof=1) / len(g) ** 0.5 if len(g) > 1 else float('nan')


def line(tag, s, fake=None):
    if len(s) < 12:
        return f'  {tag:26s} n {len(s):4d} too few'
    parts = [f'{s[f"thru_{h}"].mean():+5.1f}' for h in HORIZONS]
    th = (s['first'] == 'through').sum()
    bk = (s['first'] == 'back').sum()
    txt = (f'  {tag:26s} n {len(s):4d} d {s["day"].nunique():2d} | thru5/15/30/60 ' + ' '.join(parts)
           + f' | typ dist {s["abs_30"].median():4.1f} | reach thru {s["reach_thru"].median():4.1f} back {s["reach_back"].median():4.1f}'
           + f' | thru first {100 * th / max(th + bk, 1):3.0f}%')
    if fake is not None and len(fake) >= 12:
        a, _ = ci(s, 'thru_30')
        b, _ = ci(fake, 'thru_30')
        j = pd.concat([s.groupby('day')['thru_30'].mean(), -fake.groupby('day')['thru_30'].mean()], axis=1).dropna()
        d = j.sum(axis=1)
        txt += f' | vs fake 30m {a - b:+.1f} ±{1.96 * d.std(ddof=1) / max(len(d), 1) ** 0.5:.1f}'
        fth, fbk = (fake['first'] == 'through').sum(), (fake['first'] == 'back').sum()
        txt += f' (fake thru first {100 * fth / max(fth + fbk, 1):.0f}%)'
    return txt


def main():
    random_seed = random.Random(7)
    by_day = P.sessions(max_age=0)
    days = [d for d in sorted(by_day) if len(by_day[d]) >= 300]
    es = pd.read_csv(os.path.join(gex_stats._DATA_DIR, 'price_history_ES.csv'), usecols=['datetime', 'volume'])
    es['t'] = es['datetime'] // 1000
    es_t, es_v = es['t'].to_numpy(), es['volume'].to_numpy(float)
    rows = []
    for day in days:
        if not P.snapshots(day):
            continue
        bars = by_day[day]
        lv, gex_at, net = levels_by_bar(day, bars, by_day, days)
        lines = cm.load_day(day)
        lt = [datetime.fromisoformat(x['ts']).timestamp() for x in lines]
        a, b = np.searchsorted(es_t, bars[0][0]), np.searchsorted(es_t, bars[-1][0])
        day_v = float(np.median(es_v[a:b])) if b > a else 0.0

        def es_vol(t):
            i = np.searchsorted(es_t, t)
            return float(es_v[max(i - 5, 0):i].mean() / day_v) if day_v and i else np.nan
        rows += scan(day, bars, lv, gex_at, net, None, lt, lines, es_vol, random_seed)
        print(day, len(rows), flush=True)
    df = pd.DataFrame(rows)
    out = os.path.join(os.environ.get('TEMP', '.'), 'level_moves.csv')
    df.to_csv(out, index=False)
    real, fake = df[df['real']], df[~df['real']]
    print(f'\n{len(real)} real approaches, {len(fake)} fake, {df["day"].nunique()} days. + = kept going through the level, - = bounced (pts, mean)')
    order = real['level'].value_counts().index
    print('\n== by level (typ dist = median distance from entry after 30 min, either way)')
    for lv in order:
        print(line(lv, real[real['level'] == lv], fake[fake['level'] == lv]))
    print(line('every fake level', fake))
    g = real[real['level'] != 'round 25']
    print('\n== every real level, by what else was known')
    splits = [('from above (downward)', g['frm'] == 'above'), ('from below (upward)', g['frm'] == 'below'),
              ('net GEX +', g['net'] == 'pos'), ('net GEX -', g['net'] == 'neg'),
              ('GEX at strike +', g['gex_strike'] == 'pos'), ('GEX at strike -', g['gex_strike'] == 'neg'),
              ('before 11:00', g['m'] < 90), ('11:00-14:00', (g['m'] >= 90) & (g['m'] < 270)), ('after 14:00', g['m'] >= 270),
              ('ES volume high', g['es_vol'] > g['es_vol'].median()), ('ES volume low', g['es_vol'] <= g['es_vol'].median()),
              ('fast approach', g['speed'] > g['speed'].median()), ('slow approach', g['speed'] <= g['speed'].median()),
              ('charm with the move', g['charm'] == 'with'), ('charm against', g['charm'] == 'against')]
    for name, mask in splits:
        print(line(name, g[mask]))
    print('\n== the strike-GEX levels by their GEX and the regime')
    k = g[g['gex_strike'].notna()]
    for s in ('pos', 'neg'):
        for n in ('pos', 'neg'):
            print(line(f'strike {s} net {n}', k[(k['gex_strike'] == s) & (k['net'] == n)]))
    for lv in ('trapdoor', 'gold', 'gex wall', 'squeeze', 'support', 'resistance', 'pin', 'true_pin'):
        for s in ('pos', 'neg'):
            x = g[(g['level'] == lv) & (g['gex_strike'] == s)]
            if len(x) >= 12:
                print(line(f'{lv} / strike {s}', x))


if __name__ == '__main__':
    main()
