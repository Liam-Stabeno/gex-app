"""
Can we call the day's top or bottom while it's happening? Live, a low only looks like the low
until a lower one comes, so the test is the question the screen asks: each time SPX futures
print a new session high or low (10:00-15:30 ET, one per side per COOLDOWN_MIN), does it hold?

  holds    no bar goes more than HOLD_PTS through it for the rest of the session
  turn     from that bar's close, the first moment anyone could act on it, price goes
           TURN_ATR of a usual day's range back the other way before STOP_ATR further on
           (the tradable version: it needn't be the day's extreme; chance is 1/3). Measured
           from the extreme instead, a long wick has already banked part of the move.

Part 1, ES 1-minute bars since 2024 (hundreds of sessions): a walk-forward LightGBM on price and
volume against the same model on the clock alone, since later extremes hold more often anyway.
AUC 0.50 = no idea. Each 20 sessions are scored by a model fitted only on the 250 before them.

Part 2, the GEX and charm days: the same events with the levels in force at that minute. With
so few days these are plain hold rates, against the rate at the same time of day, with an
error bar over days.

Part 3, at 10:00 where the day's high and low will land: how often within NEAR_PTS of a level
in force at 10:00, against the same levels shifted by a random amount (chance).

Run:  python tools/turning_point_test.py      (the system python: it has lightgbm + pandas)
"""
import glob
import math
import os
import random
import sys
from datetime import time as dtime

import numpy as np
import pandas as pd

ROOT = r'C:\Productivity\TradingApp'
DATA = os.path.join(ROOT, 'data')
ET = 'America/New_York'
HOLD_PTS, TURN_ATR, STOP_ATR = 2.0, 0.20, 0.10
COOLDOWN_MIN, FIRST_MIN, LAST_MIN = 10, 30, 360
TRAIN_DAYS, STEP_DAYS, TRAIL_DAYS = 250, 20, 20
NEAR_PTS = 5.0
PRICE_FEATURES = ['m', 'range_atr', 'speed5', 'speed15', 'vol_ratio', 'vol5_30', 'wick', 'from_open',
                  'to_prev_x', 'gap', 'n_new', 'side_up', 'dvol15']
CLOCK = ['m', 'side_up']


def load_es():
    d = pd.read_csv(os.path.join(DATA, 'price_history_ES.csv'))
    d['dt'] = pd.to_datetime(d['datetime'], unit='ms', utc=True).dt.tz_convert(ET)
    d = d[(d['dt'].dt.time >= dtime(9, 30)) & (d['dt'].dt.time < dtime(16, 0)) & (d['dt'].dt.weekday < 5)].copy()
    d['day'] = d['dt'].dt.date.astype(str)
    d['m'] = (d['dt'].dt.hour - 9) * 60 + d['dt'].dt.minute - 30
    good = d.groupby('day')['m'].transform('size') >= 370      # full sessions only
    return d[good].sort_values('datetime').reset_index(drop=True)


def events(day, g, prev, atr, usual_vol):
    """the new-extreme events of one session; every feature uses bars up to and including it"""
    o, h, l, c, v, mm = (g[k].to_numpy(float) for k in ('open', 'high', 'low', 'close', 'volume', 'm'))
    rows, last = [], {1: -99, -1: -99}
    hi, lo = np.maximum.accumulate(h), np.minimum.accumulate(l)
    counts = {1: 0, -1: 0}
    signed = (c - o) * v                                     # up bars' volume minus down bars', roughly
    for i in range(1, len(g)):
        m = int(mm[i])
        for up in (1, -1):
            new = h[i] > hi[i - 1] if up == 1 else l[i] < lo[i - 1]
            if not new:
                continue
            counts[up] += 1
            if not FIRST_MIN <= m <= LAST_MIN or m - last[up] < COOLDOWN_MIN or i < 30:
                continue
            last[up] = m
            x = h[i] if up == 1 else l[i]
            later_h, later_l = h[i + 1:], l[i + 1:]
            holds = bool(len(later_h) and (later_h.max() <= x + HOLD_PTS if up == 1 else later_l.min() >= x - HOLD_PTS))
            turn = None
            for a, b in zip(later_h, later_l):
                good = (c[i] - b >= TURN_ATR * atr) if up == 1 else (a - c[i] >= TURN_ATR * atr)
                bad = (a - c[i] >= STOP_ATR * atr) if up == 1 else (c[i] - b >= STOP_ATR * atr)
                if bad:
                    turn = 0
                    break
                if good:
                    turn = 1
                    break
            bar = max(h[i] - l[i], 0.25)
            rows.append(dict(
                day=day, m=m, side_up=int(up == 1), holds=int(holds), turn=turn, px=x,
                range_atr=(hi[i] - lo[i]) / atr, speed5=up * (c[i] - c[i - 5]) / atr, speed15=up * (c[i] - c[i - 15]) / atr,
                vol_ratio=v[i - 4:i + 1].mean() / max(np.mean(usual_vol[max(m - 4, 0):m + 1]), 1.0),
                vol5_30=v[i - 4:i + 1].mean() / max(v[i - 29:i + 1].mean(), 1.0),
                wick=((h[i] - c[i]) if up == 1 else (c[i] - l[i])) / bar,
                from_open=up * (c[i] - o[0]) / atr, gap=up * (o[0] - prev['close']) / atr,
                to_prev_x=up * (x - (prev['high'] if up == 1 else prev['low'])) / atr, n_new=counts[up],
                dvol15=up * signed[i - 14:i + 1].sum() / max(np.abs(signed[i - 14:i + 1]).sum(), 1.0)))
    return rows


def build_events(d):
    days = sorted(d['day'].unique())
    groups = {k: g for k, g in d.groupby('day')}
    ranges, vols, rows = [], [], []
    for n, day in enumerate(days):
        g = groups[day]
        if len(ranges) >= TRAIL_DAYS:
            prev = dict(high=g0['high'].max(), low=g0['low'].min(), close=g0['close'].iloc[-1])  # noqa: F821
            usual = np.nanmedian(np.array(vols[-TRAIL_DAYS:]), axis=0)
            rows += events(day, g, prev, float(np.median(ranges[-TRAIL_DAYS:])), usual)
        ranges.append(g['high'].max() - g['low'].min())
        v = np.full(390, np.nan)
        v[g['m'].to_numpy(int).clip(0, 389)] = g['volume'].to_numpy(float)
        vols.append(v)
        g0 = g
    return pd.DataFrame(rows)


def auc(y, s):
    y, s = np.asarray(y), np.asarray(s)
    r = pd.Series(s).rank().to_numpy()
    n1, n0 = int(y.sum()), int(len(y) - y.sum())
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else float('nan')


def walk_forward(e, target, features):
    import lightgbm as lgb
    e = e.dropna(subset=[target])
    days = sorted(e['day'].unique())
    out = []
    for start in range(TRAIN_DAYS, len(days), STEP_DAYS):
        tr = e[e['day'].isin(days[start - TRAIN_DAYS:start])]
        te = e[e['day'].isin(days[start:start + STEP_DAYS])]
        if te.empty:
            continue
        m = lgb.LGBMClassifier(n_estimators=120, learning_rate=0.03, num_leaves=7, min_child_samples=60,
                               subsample=0.8, subsample_freq=1, colsample_bytree=0.8, verbose=-1, random_state=1)
        m.fit(tr[features], tr[target])
        out.append(te.assign(p=m.predict_proba(te[features])[:, 1]))
    return pd.concat(out) if out else pd.DataFrame()


def day_error(df, col):
    """rate and 95% error bar with one session as one observation"""
    g = df.groupby('day')[col].mean()
    return 100 * df[col].mean(), 196 * g.std(ddof=1) / max(len(g), 1) ** 0.5 if len(g) > 1 else float('nan')


def part1(e):
    print(f'\n== Part 1: ES since 2024: {len(e)} new-extreme events, {e["day"].nunique()} sessions, {e["day"].min()} .. {e["day"].max()}')
    print(f'holds {100 * e["holds"].mean():.0f}%, tradable turn {100 * e["turn"].mean():.0f}% of decided')
    for lo_, hi_ in ((30, 120), (120, 240), (240, 330), (330, 361)):
        s = e[(e['m'] >= lo_) & (e['m'] < hi_)]
        print(f'  {lo_ / 60 + 9.5:5.1f}h-{hi_ / 60 + 9.5:5.1f}h  n {len(s):5d}  holds {100 * s["holds"].mean():4.0f}%  turn {100 * s["turn"].mean():4.0f}%')
    passed = []
    for t in ('holds', 'turn'):
        a = walk_forward(e, t, PRICE_FEATURES)
        b = walk_forward(e, t, CLOCK)
        j = a.assign(clock=b['p'].to_numpy())
        print(f'\n{t}: walk-forward on {j["day"].nunique()} sessions, {len(j)} events')
        print(f'  AUC price + volume {auc(j[t], j["p"]):.3f}   clock alone {auc(j[t], j["clock"]):.3f}')
        by_day = [auc(g[t], g['p']) - auc(g[t], g['clock']) for _, g in j.groupby('day') if g[t].nunique() > 1 and len(g) >= 4]
        by_day = [x for x in by_day if x == x]
        edge, bar = np.mean(by_day), 1.96 * np.std(by_day, ddof=1) / len(by_day) ** 0.5
        print(f'  edge over clock per day {edge:+.3f} ± {bar:.3f} ({len(by_day)} days)')
        j['bin'] = pd.qcut(j['p'], 10, labels=False, duplicates='drop')
        top = j[j['bin'] == j['bin'].max()]
        same = j.loc[j['bin'] != j['bin'].max()]
        print(f'  most confident 10%: {100 * top[t].mean():.0f}% (n {len(top)}, {top["day"].nunique()} days) vs clock-matched '
              f'{100 * top["clock"].mean():.0f}%, the rest {100 * same[t].mean():.0f}%')
        # the later half on its own: did it last?
        days = sorted(j['day'].unique())
        late = j[j['day'] >= days[len(days) // 2]]
        print(f'  later half alone: AUC {auc(late[t], late["p"]):.3f} vs clock {auc(late[t], late["clock"]):.3f}')
        if edge > bar and auc(late[t], late['p']) > auc(late[t], late['clock']):
            passed.append(f'{t}: price + volume beat the clock')
        if t == 'turn':
            print('  by side: ' + ', '.join(f'{"tops" if s else "bottoms"} {auc(g[t], g["p"]):.3f}' for s, g in j.groupby('side_up')))
            imp = j[PRICE_FEATURES + [t]].corr(method='spearman')[t].drop(t).sort_values(key=abs, ascending=False).head(6)
            print('  strongest single inputs (rank corr):', {k: round(v, 3) for k, v in imp.items()})
    print('RESULT: 2 tests; about 0.1 by chance; passed: ' + (' | '.join(passed) or 'none'))
    return e


def levels_at(day, ts_local):
    """the levels in force at that local time from the day's snapshots, or None"""
    p = os.path.join(DATA, f'gex_snapshots_SPX_{day}.csv')
    if not os.path.exists(p):
        return None
    s = pd.read_csv(p)
    s = s[s['timestamp'] <= ts_local]
    if s.empty or s['timestamp'].iloc[-1] < ts_local[:11] + f'{int(ts_local[11:13]) - 1:02d}' + ts_local[13:]:
        return None
    return s.iloc[-1]


def part3():
    print('\n== Part 3: is the day high / low near a level known at 10:00 ET (7:00 PT)?')
    d = pd.read_csv(os.path.join(DATA, 'price_history_SPX.csv'))
    d['dt'] = pd.to_datetime(d['datetime'], unit='ms', utc=True).dt.tz_convert(ET)
    d = d[(d['dt'].dt.time >= dtime(9, 30)) & (d['dt'].dt.time < dtime(16, 0))]
    d['day'] = d['dt'].dt.date.astype(str)
    random.seed(1)
    keys = ('put_wall', 'call_wall', 'pin', 'flip_level', 'true_pin')
    rows = []
    for f in sorted(glob.glob(os.path.join(DATA, 'gex_snapshots_SPX_2*.csv'))):
        day = f[-14:-4]
        g = d[d['day'] == day]
        if len(g) < 380:
            continue
        r = levels_at(day, f'{day} 07:00:00')
        if r is None:
            continue
        after = g[g['dt'].dt.time >= dtime(10, 0)]
        hi, lo = after['high'].max(), after['low'].min()
        ls =[float(r[k]) for k in keys if k in r and pd.notna(r[k])]
        if not ls:
            continue
        near = lambda x, xs: min(abs(x - v) for v in xs) <= NEAR_PTS
        shifted = [[v + s for v in ls] for s in [random.uniform(-40, 40) for _ in range(200)]]
        rows.append(dict(day=day, hi=near(hi, ls), lo=near(lo, ls),
                         hi_chance=np.mean([near(hi, x) for x in shifted]), lo_chance=np.mean([near(lo, x) for x in shifted]),
                         n=len(ls), **{f'd_{k}': (lo - float(r[k])) if k in r and pd.notna(r[k]) else np.nan for k in keys}))
    t = pd.DataFrame(rows)
    print(f'{len(t)} days; within {NEAR_PTS:g} pts of any level: day high {100 * t["hi"].mean():.0f}% vs chance {100 * t["hi_chance"].mean():.0f}%; '
          f'day low {100 * t["lo"].mean():.0f}% vs chance {100 * t["lo_chance"].mean():.0f}%')


def part2(e):
    """the GEX days: hold rate near each level at that minute vs other events of that hour"""
    print('\n== Part 2: new extremes at GEX levels (SPX levels vs ES prices, basis taken out)')
    rows = []
    for f in sorted(glob.glob(os.path.join(DATA, 'gex_snapshots_SPX_2*.csv'))):
        day = f[-14:-4]
        sub = e[e['day'] == day]
        if sub.empty:
            continue
        s = pd.read_csv(f)
        s['t'] = pd.to_datetime(s['timestamp']).dt.tz_localize('America/Los_Angeles', ambiguous='NaT', nonexistent='NaT')
        s = s.dropna(subset=['t']).sort_values('t')
        open_t = pd.Timestamp(f'{day} 09:30', tz=ET)
        for _, ev in sub.iterrows():
            t = open_t + pd.Timedelta(minutes=int(ev['m']))
            i = s['t'].searchsorted(t, side='right') - 1
            if i < 0 or (t - s['t'].iloc[i]).total_seconds() > 300:
                continue
            r = s.iloc[i]
            basis = float(ev['es_close']) - float(r['spot'])
            x = ev['px'] - basis
            levels = {k: float(r[k]) for k in ('put_wall', 'call_wall', 'pin', 'flip_level', 'true_pin') if k in r and pd.notna(r[k])}
            hit = [k for k, v in levels.items() if abs(x - v) <= NEAR_PTS]
            rows.append(dict(day=day, m=ev['m'], side_up=ev['side_up'], holds=ev['holds'], turn=ev['turn'], neg=float(r['total_gex']) < 0,
                             any_level=bool(hit), **{f'at_{k}': k in hit for k in levels}))
    if not rows:
        print('no events'); return
    t = pd.DataFrame(rows)
    print(f'{len(t)} events, {t["day"].nunique()} days')
    for name, mask in [('all', t['holds'] >= 0), ('at any level', t['any_level']), ('at no level', ~t['any_level']),
                       ('net GEX positive', ~t['neg']), ('net GEX negative', t['neg'])] + \
            [(c, t[c].fillna(False).astype(bool)) for c in t.columns if c.startswith('at_')]:
        s = t[mask]
        if len(s) < 8:
            print(f'  {name:18s} n {len(s)} too few'); continue
        a, ae = day_error(s, 'holds')
        b, be = day_error(s.dropna(subset=['turn']).assign(turn=lambda x: x['turn'].astype(float)), 'turn')
        print(f'  {name:18s} n {len(s):4d} days {s["day"].nunique():3d}  holds {a:3.0f}% ±{ae:.0f}  turn {b:3.0f}% ±{be:.0f}')


def main():
    d = load_es()
    e = build_events(d)
    closes = d.groupby(['day', 'm'])['close'].first()
    e['es_close'] = [closes.get((a, b), np.nan) for a, b in zip(e['day'], e['m'])]
    part1(e)
    part2(e)
    part3()
    out = os.path.join(os.environ.get('TEMP', '.'), 'turning_point_events.csv')
    e.to_csv(out, index=False)
    print('rows:', out)


if __name__ == '__main__':
    sys.exit(main())
