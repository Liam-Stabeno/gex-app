"""
Can the 0DTE charm map call what SPX does NEXT? Every 15 min from 10:00 on every saved charm
map day (data/charm_map_SPX_<date>, src/charm_map.py), measured from that map and from bars
before its time only:

  flow_pct    flow at price against this clock time's last 20 days (the raw sign is BUY ~92%
              of the time, so only its size can carry information)
  drift       charm flow / |0DTE gamma|: pts per hour price would have to move for gamma's
              hedging to cancel the decay's
  slope       how fast the flow falls with price, ±10 pts around spot, per $ of flow: + = decay
              pulls price back to here, - = pushes it away
  asym        flow on the prices above spot against below
  d_flow      change in the flow over the last 15 min
  to_pull, to_split   signed distance to the nearest of each (0 when none within the map)

Each is rank-correlated with the next move per day (signed return), and with the next range
over its usual (the scoreboard's baseline), then averaged over days: t = mean / se, so one
day is one observation. Half the days pick, the later half check: a signal counts only if
it has the same sign and |t| >= 2 in both.

Run:  python tools/charm_signal_test.py      (the .venv)
"""
import math
import os
import sys
from bisect import bisect_right
from datetime import datetime

import numpy as np
import pandas as pd

ROOT = r'C:\Productivity\TradingApp'
sys.path.insert(0, ROOT + r'\src')
os.chdir(ROOT)
import charm_map as cm  # noqa: E402
import predictions as P  # noqa: E402

HORIZONS = (15, 30, 60)
EVERY_MIN, FIRST_MIN, LAST_MIN = 15, 30, 345      # 10:00 .. 15:15 after the open
TRAIL_DAYS = 20
FRESH_SEC = 300
MIN_POINTS = 6                                     # checkpoints a day needs to give a correlation
FEATURES = ('flow_pct', 'drift', 'slope', 'asym', 'd_flow', 'to_pull', 'to_split')


def gamma_of(line, spot):
    """the dealers' 0DTE gamma in $ of delta per point (long calls, short puts)"""
    t, g = line['T'], 0.0
    for k, s, oi, v in line['book']:
        sq = v * math.sqrt(t)
        if sq <= 0:
            continue
        d1 = (math.log(spot / k) + (cm.R + 0.5 * v * v) * t) / sq
        g += (1 if s == 'c' else -1) * math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * spot * sq) * oi * 100 * spot
    return g


def features(line, prior):
    flow = np.array(line['flow'], float)
    n = len(flow) // 2
    f0 = float(flow[n])
    scale = float(np.abs(flow).mean()) or 1.0
    spot = line['spot']
    g = gamma_of(line, spot)
    near = lambda xs: (xs[0] - spot) if xs else 0.0
    return dict(f0=f0, g0=g, drift=f0 / abs(g) if g else 0.0, slope=(flow[n - 10] - flow[n + 10]) / 20 / scale,
                asym=(flow[n + 1:].sum() - flow[:n].sum()) / (np.abs(flow).sum() or 1.0),
                d_flow=(f0 - prior) / scale if prior is not None else np.nan,
                to_pull=near(line['pull']), to_split=near(line['split']))


def build():
    by_day = P.sessions(max_age=0)
    rows = []
    for day in sorted(by_day):
        lines = cm.load_day(day)
        bars = by_day[day]
        if not lines or len(bars) < 300:
            continue
        ts = [datetime.fromisoformat(x['ts']).timestamp() for x in lines]
        t_open = P._open_ts(day)
        base = P.baselines(day, by_day)
        for m in range(FIRST_MIN, LAST_MIN + 1, EVERY_MIN):
            t0 = t_open + 60 * m
            i = bisect_right(ts, t0) - 1
            if i < 0 or t0 - ts[i] > FRESH_SEC:
                continue
            j = bisect_right(ts, t0 - 900) - 1
            prior = lines[j]['flow'][len(lines[j]['flow']) // 2] if j >= 0 and t0 - 900 - ts[j] <= FRESH_SEC else None
            r = dict(day=day, m=m, **features(lines[i], prior))
            first = P._window(bars, t0, 1)
            if not first:
                continue
            px = first[0][1]
            prev = P._window(bars, t0 - 900, 15)
            b15 = base.get((m - 15, 15))
            r['recent'] = P._range(prev) / b15 if b15 and len(prev) >= 13 else np.nan
            for h in HORIZONS:
                w = P._window(bars, t0, h)
                if len(w) >= 0.9 * h and (m, h) in base:
                    r[f'ret{h}'] = w[-1][4] - px
                    r[f'size{h}'] = P._range(w) / base[(m, h)]
            r['ret_close'] = bars[-1][4] - px
            rows.append(r)
    df = pd.DataFrame(rows)
    # the flow against this clock time's own last 20 days, known at the time
    df['flow_pct'] = np.nan
    for m, g in df.groupby('m'):
        g = g.sort_values('day')
        hist, out = [], []
        for v in g['f0']:
            out.append(np.mean([x < v for x in hist[-TRAIL_DAYS:]]) if len(hist) >= 5 else np.nan)
            hist.append(v)
        df.loc[g.index, 'flow_pct'] = out
    return df


def day_t(df, x, y):
    """(mean daily rank correlation, t, days)"""
    v = []
    for _, g in df.dropna(subset=[x, y]).groupby('day'):
        if len(g) >= MIN_POINTS and g[x].nunique() > 2:
            v.append(g[x].rank().corr(g[y].rank()))
    v = [c for c in v if c == c]
    if len(v) < 5:
        return np.nan, np.nan, len(v)
    return float(np.mean(v)), float(np.mean(v) / (np.std(v, ddof=1) / len(v) ** 0.5)), len(v)


def main():
    df = build()
    days = sorted(df['day'].unique())
    cut = days[len(days) // 2]
    print(f'{len(df)} checkpoints, {len(days)} days, {days[0]} .. {days[-1]}; picking {days[0]}..{cut}, checking after')
    print(f'\nflow BUY at {100 * (df["f0"] > 0).mean():.0f}% of checkpoints; 0DTE gamma positive at {100 * (df["g0"] > 0).mean():.0f}%')
    targets = [(f'ret{h}', f'next {h} direction') for h in HORIZONS] + [('ret_close', 'to the close')] \
        + [(f'size{h}', f'next {h} size') for h in HORIZONS]
    passed, tested = [], 0
    a, b = df[df['day'] < cut], df[df['day'] >= cut]
    for y, name in targets:
        print(f'\n{name:20s} first half       later half')
        for x in FEATURES + (('recent',) if y.startswith('size') else ()):
            r1, t1, n1 = day_t(a, x, y)
            r2, t2, n2 = day_t(b, x, y)
            tested += x != 'recent'
            ok = abs(t1) >= 2 and abs(t2) >= 2 and t1 * t2 > 0
            print(f'  {x:10s} {r1:+.3f} t {t1:+5.1f} n{n1:<3d}  {r2:+.3f} t {t2:+5.1f} n{n2:<3d} {"<- holds" if ok else ""}')
            if ok and x != 'recent':
                passed.append((y, x, round(r1, 3), round(t1, 1), round(r2, 3), round(t2, 1)))
    print(f'\n{tested} tests; by chance about {tested * 0.0025:.1f} pass both halves')
    print('passed:', passed or 'none')
    print(f'RESULT: {tested} tests; about {tested * 0.0025:.1f} by chance; passed: '
          + (' | '.join(f'{x} on {y}' for y, x, *_ in passed) or 'none'))
    # does a pass add anything to the recent range, which needs no options data?
    for y, x, *_ in passed:
        if y.startswith('size'):
            d = df.dropna(subset=[x, y, 'recent']).copy()
            d['res'] = d[y] - np.polyval(np.polyfit(d['recent'], d[y], 1), d['recent'])
            print(f'  {x} on {y} after taking out the recent range:', [round(v, 3) if isinstance(v, float) else v for v in day_t(d, x, 'res')])
    out = os.path.join(os.environ.get('TEMP', '.'), 'charm_signal_rows.csv')
    df.to_csv(out, index=False)
    print('rows:', out)


if __name__ == '__main__':
    main()
