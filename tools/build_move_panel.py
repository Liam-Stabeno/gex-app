"""
One table of everything recorded, every 5 min of every SPX session: what was known at that
moment and what happened next, for tools/move_model_test.py. Every input uses data before
the checkpoint only. Blocks are blank on days from before that recorder started.

  price     clock, recent ranges vs usual, recent returns, place in the day's range
  es        ES volume vs usual at this clock time, up-bar vs down-bar volume
  gex       net GEX, distance to the walls, pin, True Pin, flip (snapshots, from 2026-06)
  grid      GEX at the price, above vs below it, the biggest strike (heatmap, from 10/06)
  charm     the 0DTE charm map (charm_signal_test.py, from 2026-06)
  flow      ES buy vs sell volume by the tick rule (volume_split, from 10/06)
  book      ES book imbalance, spread, large trades (es_book, from 10/13)
  internals market internals, change over 15 min (internals, from 10/14)

Targets, for the next 5 / 15 / 30 / 60 min: big_h (range >= 1.5x usual for this clock time and
length), up_h (closes higher); low_next_h, high_next_h (the day's low / high prints in it).

Run:  python tools/build_move_panel.py      (the .venv) -> %TEMP%\\move_panel.csv
"""
import csv
import glob
import os
import re
import statistics
import sys
from bisect import bisect_right
from datetime import date, datetime

import numpy as np
import pandas as pd

ROOT = r'C:\Productivity\TradingApp'
sys.path.insert(0, ROOT + r'\src')
sys.path.insert(0, ROOT + r'\tools')
os.chdir(ROOT)
import charm_map as cm  # noqa: E402
import charm_signal_test as cs  # noqa: E402
import gex_stats  # noqa: E402
import predictions as P  # noqa: E402
from gex_stats import day_file, open_text  # noqa: E402

DATA = gex_stats._DATA_DIR
HORIZONS = (5, 15, 30, 60)
EVERY_MIN, FIRST_MIN = 5, 15
BIG = 1.5
BASE_DAYS = 20
FRESH_SEC = 300
LEVELS = ('put_wall', 'call_wall', 'pin', 'true_pin', 'flip_level')


def usual(days, by_min):
    """{(m, h): median range} over the given days' per-minute (high, low)"""
    out = {}
    for h in (5, 15, 30, 60):
        for m in range(0, 390 - h + 1, 5):
            v = []
            for mins in days:
                w = [x for x in by_min[mins][m:m + h] if x]
                if len(w) >= 0.8 * h:
                    v.append(max(x[0] for x in w) - min(x[1] for x in w))
            if len(v) >= 5:
                out[(m, h)] = statistics.median(v)
    return out


def minute_csv(name, cols):
    """{epoch minute: row} of a day file with a `time` column in epoch seconds"""
    path = day_file(name)
    if not path:
        return {}
    out = {}
    with open_text(path) as f:
        for r in csv.DictReader(f):
            try:
                out[int(float(r['time'])) // 60] = {c: float(r[c] or 0) for c in cols}
            except (KeyError, ValueError):
                continue
    return out


def internals(day):
    path = day_file(f'internals_{day}.csv')
    if not path:
        return None
    d = pd.read_csv(path, usecols=['time', 'symbol', 'close'])
    d['min'] = d['time'].astype(int) // 60
    p = d.pivot_table(index='min', columns='symbol', values='close', aggfunc='last').sort_index().ffill()
    p.columns = [re.sub(r'\W', '', c) for c in p.columns]
    return p


def es_minutes():
    d = pd.read_csv(os.path.join(DATA, 'price_history_ES.csv'), usecols=['datetime', 'open', 'close', 'volume'])
    return d.assign(min=(d['datetime'] // 60000).astype('int64')).set_index('min')


def main():
    by_day = P.sessions(max_age=0)
    days = [d for d in sorted(by_day) if len(by_day[d]) >= 300]
    by_min = {d: P._by_minute(d, by_day[d]) for d in days}
    es = es_minutes()
    es_day = {}
    rows = []
    for n, day in enumerate(days):
        if n < 5:
            continue
        bars = by_day[day]
        t_open = P._open_ts(day)
        prior = days[max(0, n - BASE_DAYS):n]
        base = usual(prior, by_min)
        atr = statistics.median(max(b[2] for b in by_day[d]) - min(b[3] for b in by_day[d]) for d in prior)
        snaps = P.snapshots(day)
        snap_t = [x[0] for x in snaps]
        snaps0 = P.snapshots(day, zero_dte=True)
        heat = gex_stats.load_heatmap('SPX', day=date.fromisoformat(day))
        tl = P.level_timeline(heat) if heat.get('times') else []
        tl_t = [x[0] for x in tl]
        strikes = np.array(heat.get('strikes') or [], float)
        ht = [int(t) for t in heat.get('times', [])]
        lines = cm.load_day(day)
        line_t = [datetime.fromisoformat(x['ts']).timestamp() for x in lines]
        split = minute_csv(f'volume_split_ES_{day}.csv', ('buy', 'sell'))
        book = minute_csv(f'es_book_{day}.csv', ('imbalance_avg', 'spread_avg', 'volume', 'large_volume'))
        ints = internals(day)
        closes = [b[4] for b in bars]
        times = [b[0] for b in bars]
        dmin = np.array(es.index[(es.index >= t_open // 60) & (es.index < t_open // 60 + 390)])
        es_day[day] = es.loc[dmin, 'volume'].groupby(((dmin - t_open // 60) // 5) * 5).sum() if len(dmin) else pd.Series(dtype=float)
        vol_hist = [es_day[d] for d in prior if d in es_day and len(es_day[d])]
        day_lo, day_hi = min(b[3] for b in bars), max(b[2] for b in bars)
        lo_t = next(b[0] for b in bars if b[3] == day_lo)
        hi_t = next(b[0] for b in bars if b[2] == day_hi)
        prev = by_day[days[n - 1]]
        for m in range(FIRST_MIN, 390 - 5 + 1, EVERY_MIN):
            t0 = t_open + 60 * m
            i = bisect_right(times, t0 - 1)
            if i < 15 or i >= len(bars):
                continue
            px = bars[i][1]
            past = bars[:i]
            r = dict(day=day, m=m, px=px, atr=atr)
            sess_hi, sess_lo = max(b[2] for b in past), min(b[3] for b in past)
            for k in (5, 15, 30):
                w = P._window(bars, t0 - 60 * k, k)
                b = base.get((m - k, k)) if m >= k else None
                r[f'recent{k}'] = P._range(w) / b if b and w else np.nan
                r[f'r{k}'] = (px - closes[max(i - k, 0)]) / atr
            r.update(pos=(px - sess_lo) / max(sess_hi - sess_lo, 0.25), range_atr=(sess_hi - sess_lo) / atr,
                     to_hi=(sess_hi - px) / atr, to_lo=(px - sess_lo) / atr,
                     to_prev_close=(px - prev[-1][4]) / atr, to_prev_hi=(px - max(b[2] for b in prev)) / atr,
                     to_prev_lo=(px - min(b[3] for b in prev)) / atr)
            # es
            mk = t0 // 60
            e = es.loc[(es.index >= mk - 30) & (es.index < mk)]
            if len(e) >= 20:
                v = e['volume'].to_numpy(float)
                usual_v = [x.get(m - 5) for x in vol_hist if m >= 5 and x.get(m - 5) is not None]
                r['es_vol5'] = v[-5:].sum() / statistics.median(usual_v) if len(usual_v) >= 5 and statistics.median(usual_v) else np.nan
                r['es_vol5_30'] = v[-5:].mean() / max(v.mean(), 1.0)
                sg = (e['close'] - e['open']).to_numpy(float) * v
                r['es_dvol15'] = sg[-15:].sum() / max(np.abs(sg[-15:]).sum(), 1.0)
            # gex
            j = bisect_right(snap_t, t0) - 1
            if j >= 0 and t0 - snap_t[j] <= FRESH_SEC:
                s = snaps[j][1]
                num = lambda k: float(s[k]) if s.get(k) not in (None, '') else np.nan
                r['gex_net'] = num('total_gex') / 1e9
                ds = [(num(k) - px) / atr for k in LEVELS]
                for k, d in zip(LEVELS, ds):
                    r[f'd_{k}'] = d
                r['near_level'] = np.nanmin(np.abs(ds)) if not np.all(np.isnan(ds)) else np.nan
            # grid
            j = bisect_right(tl_t, t0) - 1
            if j >= 0 and t0 - tl_t[j] <= FRESH_SEC:
                r['g_at'] = tl[j][3] / 1e9
                c = bisect_right(ht, tl_t[j]) - 1
                col = np.array(heat['all'][c], float)
                up = (strikes > px) & (strikes <= px + 25)
                dn = (strikes < px) & (strikes >= px - 25)
                r['g_up'], r['g_dn'] = col[up].sum() / 1e9, col[dn].sum() / 1e9
                big = strikes[int(np.abs(col).argmax())]
                r['d_gold'] = (big - px) / atr
                names = tl[j][1]
                for nm in ('support', 'resistance', 'trapdoor', 'squeeze'):
                    ks = [k for k, v in names.items() if nm in v]
                    r[f'd_{nm}'] = (ks[0] - px) / atr if ks else np.nan
            # charm
            j = bisect_right(line_t, t0) - 1
            if j >= 0 and t0 - line_t[j] <= FRESH_SEC:
                q = bisect_right(line_t, t0 - 900) - 1
                pf = lines[q]['flow'][len(lines[q]['flow']) // 2] if q >= 0 and t0 - 900 - line_t[q] <= FRESH_SEC else None
                f = cs.features(lines[j], pf)
                r.update({f'ch_{k}': float(v) / (1e9 if k in ('f0', 'g0') else 1) for k, v in f.items()})
            # flow
            b5 = [split.get(k) for k in range(mk - 5, mk)]
            b15 = [split.get(k) for k in range(mk - 15, mk)]
            if sum(x is not None for x in b15) >= 10:
                bs = lambda xs, f: sum(x[f] for x in xs if x)
                r['fl_imb15'] = (bs(b15, 'buy') - bs(b15, 'sell')) / max(bs(b15, 'buy') + bs(b15, 'sell'), 1.0)
                r['fl_imb5'] = (bs(b5, 'buy') - bs(b5, 'sell')) / max(bs(b5, 'buy') + bs(b5, 'sell'), 1.0)
            # book
            b = [book.get(k) for k in range(mk - 5, mk)]
            b = [x for x in b if x]
            if len(b) >= 3:
                r['bk_imb'] = float(np.mean([x['imbalance_avg'] for x in b]))
                r['bk_spread'] = float(np.mean([x['spread_avg'] for x in b]))
                r['bk_large'] = sum(x['large_volume'] for x in b) / max(sum(x['volume'] for x in b), 1.0)
            # internals
            if ints is not None:
                now = ints[ints.index < mk]
                if len(now) > 15:
                    for c in ints.columns:
                        s = now[c].dropna()
                        if len(s) > 15:
                            sd = float(s.diff(15).std()) or 1.0
                            r[f'in_{c}'] = float(s.iloc[-1] - s.iloc[-16]) / sd
            # what happened next
            for h in HORIZONS:
                w = P._window(bars, t0, h)
                b = base.get((m, h))
                if len(w) >= 0.8 * h and m + h <= 390:
                    if b:
                        r[f'size_{h}'] = P._range(w) / b
                        r[f'big_{h}'] = int(r[f'size_{h}'] >= BIG)
                    if w[-1][4] != px:
                        r[f'up_{h}'] = int(w[-1][4] > px)
                    r[f'low_next_{h}'] = int(t0 <= lo_t < t0 + 60 * h)
                    r[f'high_next_{h}'] = int(t0 <= hi_t < t0 + 60 * h)
            rows.append(r)
        print(day, len(rows), flush=True)
    out = os.path.join(os.environ.get('TEMP', '.'), 'move_panel.csv')
    pd.DataFrame(rows).to_csv(out, index=False)
    print('rows:', out, len(rows))


if __name__ == '__main__':
    main()
