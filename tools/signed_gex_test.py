"""
Does counting each trade as bought or sold improve GEX? The page assumes dealers are long
calls and short puts. The ticks say what customers actually did today (tick_flow_test.trades,
multi-leg prints skipped), so every moment has three readings per strike:

  oi      the page's GEX: open interest under that assumption
  signed  oi, with each contract's net contracts bought so far today taken off the dealers
  flow    today's net contracts alone (what a "directionalized volume" heatmap shows)

Each uses trades and greeks from minutes already finished at that moment. The ticks cover
0DTE and the next expiration's top strikes, where nearly all the gamma sits. Stops at 15:30:
gamma and charm blow up as an expiring option's time runs out, so a reading from the last
stretch says little about positions.

Three tests. One day is one observation, and a test counts only if it clears its 95% bar
and points the same way in both halves of the days:

  size    the scoreboard's Size · GEX at price, same moments and rule: does signed or flow
          beat oi? Scored on balanced accuracy, the hit rate on the bigger moves and on the
          smaller ones averaged: moves ran bigger than usual ~70% of the time in 2026-10, so
          a plain hit rate mostly rewards saying "bigger". Within-day rank correlation below
          it: does the reading rank a day's moments, or only tell one day from another?
  charm   the dealers' 0DTE charm flow at price, BUY -> SPX up over the next 15 / 30 / 60 min
          and to the close: does it beat a coin, and does it stop reading BUY all the time?
  levels  support, resistance, walls, trapdoor, squeeze and gold rebuilt from each reading,
          against fake levels (level_move_test.py)

Run:  python tools/signed_gex_test.py      (the .venv) -> %TEMP%\\signed_gex_rows.csv
"""
import glob
import os
import random
import sys
from bisect import bisect_right
from datetime import date, datetime, time as dtime

import numpy as np
import pandas as pd

ROOT = r'C:\Productivity\TradingApp'
sys.path.insert(0, ROOT + r'\src')
sys.path.insert(0, ROOT + r'\tools')
import charm_map as cm  # noqa: E402
import gex_stats  # noqa: E402
import level_move_test as L  # noqa: E402
import predictions as P  # noqa: E402
import tick_flow_test as tf  # noqa: E402

VARIANTS = ('oi', 'signed', 'flow')
HORIZONS = P.HORIZONS
CUTOFF_MIN = 360                 # 15:30
CHARM_FROM_MIN, CHARM_EVERY_MIN = 30, 15
BAR_FILL_MIN = 5                 # a contract with no bar: its last one, for this long
SIZE_DAYS = 300                  # bars a session needs to count
FAMILIES = ('gold', 'gex wall', 'support', 'resistance', 'trapdoor', 'squeeze')


# ── Reading the dealers' position ────────────────────────────────────────────────────────
def net_bought(tr: pd.DataFrame) -> pd.DataFrame:
    """Contracts customers have net bought, per contract (columns), by the end of each minute
    (index: the minute's start)."""
    t = tr[(tr['s'] != 0) & ~tr['outside']]
    n = (t['s'] * t['dv']).astype(float).groupby([t['minute'], t['symbol']]).sum().unstack(fill_value=0.0)
    return n.sort_index().cumsum()


def latest(frame: pd.DataFrame, t: float):
    """The row of a by-minute frame for the last minute finished by t (None if none has)."""
    i = frame.index.searchsorted(t - 60, side='right') - 1
    return frame.iloc[i] if i >= 0 else None


def dealer_gex_change(bought, gamma, spot):
    """GEX per contract the trades moved, in the heatmap's units (per point). A customer buying,
    call or put, leaves dealers that much shorter gamma."""
    return -np.asarray(bought) * np.asarray(gamma) * 100.0 * spot


def positions(sign, oi, bought) -> dict:
    """Dealers' contracts in each reading. sign: +1 call, -1 put (long calls, short puts)."""
    base = np.asarray(sign) * np.asarray(oi)
    return {'oi': base, 'signed': base - bought, 'flow': -np.asarray(bought, float)}


def charm_flow(k, pos, iv, T, spot) -> float:
    """charm_map's flow at price, from a position of either sign, $ per hour (+ = dealers buy)."""
    return float(cm._flows(k, np.sign(pos), np.abs(pos), iv, np.full(len(k), T), np.array([spot]), cm.R)[0])


def load_bars(day: str) -> dict:
    b = pd.read_csv(gex_stats.day_file(f'option_bars_SPX_{day}.csv'), dtype={'symbol': str},
                    usecols=['time', 'symbol', 'expiry', 'strike', 'side', 'gamma', 'iv', 'oi'])
    wide = lambda c: b.pivot_table(index='time', columns='symbol', values=c, aggfunc='last').sort_index().ffill(limit=BAR_FILL_MIN)
    return {'gamma': wide('gamma'), 'iv': wide('iv'), 'oi': wide('oi'),
            'meta': b.groupby('symbol')[['expiry', 'strike', 'side']].last()}


def variant_columns(heat: dict, flows: pd.DataFrame, book: dict) -> dict:
    """Per heatmap snapshot, GEX per strike in each reading."""
    where = {k: i for i, k in enumerate(heat['strikes'])}
    meta, gamma = book['meta'], book['gamma']
    syms = [s for s in flows.columns if s in gamma.columns and meta.at[s, 'strike'] in where]
    at = np.array([where[meta.at[s, 'strike']] for s in syms], dtype=int)
    F, G = flows[syms], gamma[syms]
    signed, flow = [], []
    for t, sp, col in zip(heat['times'], heat['spots'], heat['all']):
        d = np.zeros(len(col))
        n, g = latest(F, t), latest(G, t)
        if n is not None and g is not None and sp:
            np.add.at(d, at, dealer_gex_change(n.to_numpy(), np.nan_to_num(g.to_numpy()), sp))
        signed.append((np.array(col, float) + d).tolist())
        flow.append(d.tolist())
    return {'oi': heat['all'], 'signed': signed, 'flow': flow}


# ── The three tests ──────────────────────────────────────────────────────────────────────
def size_rows(day: str, bars: list, base: dict, tl: dict) -> list:
    """The scoreboard's GEX-at-price moments: each reading's GEX, and was the move bigger?"""
    t_open = P._open_ts(day)
    tt = {v: [x[0] for x in tl[v]] for v in VARIANTS}
    rows = []
    for m in P._check_times(min(HORIZONS)):
        if m >= CUTOFF_MIN:
            continue
        t0 = t_open + 60 * m
        g = {}
        for v in VARIANTS:
            j = bisect_right(tt[v], t0) - 1
            if j >= 0 and t0 - tt[v][j] <= P.STALE_SEC:
                g[v] = tl[v][j][3]
        if len(g) < len(VARIANTS):
            continue
        for h in HORIZONS:
            if m + h > P.SESSION_MIN or (m, h) not in base:
                continue
            w = P._window(bars, t0, h)
            if len(w) < 0.9 * h or P._range(w) == base[(m, h)]:
                continue
            bigger = P._range(w) > base[(m, h)]
            rows.append({'day': day, 'm': m, 'h': h, 'bigger': bigger, **{f'g_{v}': g[v] for v in VARIANTS},
                         **{f'hit_{v}': (g[v] < 0) == bigger for v in VARIANTS}})
    return rows


def charm_rows(day: str, bars: list, flows: pd.DataFrame, book: dict) -> list:
    meta = book['meta']
    z = meta[meta['expiry'] == day]
    syms = [s for s in z.index if s in book['iv'].columns and s in book['oi'].columns]
    if not syms:
        return []
    k = z.loc[syms, 'strike'].to_numpy(float)
    sign = np.where(z.loc[syms, 'side'] == 'call', 1.0, -1.0)
    t_open = P._open_ts(day)
    close = bars[-1][4]
    rows = []
    for m in range(CHARM_FROM_MIN, CUTOFF_MIN + 1, CHARM_EVERY_MIN):
        t0 = t_open + 60 * m
        first = P._window(bars, t0, 1)
        iv, oi = latest(book['iv'][syms], t0), latest(book['oi'][syms], t0)
        if not first or iv is None or oi is None:
            continue
        spot = first[0][1]
        n = latest(flows.reindex(columns=syms, fill_value=0.0), t0)
        bought = np.zeros(len(syms)) if n is None else n.to_numpy(float)
        T = max(t_open + 60 * P.SESSION_MIN - t0, 300) / (cm.HOURS_PER_YEAR * 3600)
        v = iv.to_numpy(float)
        ok = v == v
        pos = positions(sign, np.nan_to_num(oi.to_numpy(float)), bought)
        flow = {name: charm_flow(k[ok], p[ok], v[ok], T, spot) for name, p in pos.items()}
        for h in (*HORIZONS, 'close'):
            if h == 'close':
                end = close
            else:
                w = P._window(bars, t0, h)
                if len(w) < 0.9 * h:
                    continue
                end = w[-1][4]
            if end != spot:
                rows.append({'day': day, 'm': m, 'h': str(h), **{f'flow_{x}': flow[x] for x in VARIANTS},
                             **{f'hit_{x}': (flow[x] > 0) == (end > spot) for x in VARIANTS}})
    return rows


def level_rows(day: str, bars: list, tl: dict, rngs: dict) -> list:
    out = []
    for v in VARIANTS:
        tt = [x[0] for x in tl[v]]
        named = [L.named_levels(tl[v], tt, b[0]) for b in bars]
        rows = L.scan(day, bars, [x[0] for x in named], [x[1] for x in named], [None] * len(bars), None,
                      [], [], lambda t: float('nan'), rngs[v])
        out += [{**r, 'variant': v} for r in rows if r['m'] < CUTOFF_MIN]
    return out


# ── Scoring ──────────────────────────────────────────────────────────────────────────────
def day_edge(a: pd.Series, b=0.5) -> tuple:
    """(mean, 95% bar, first half, later half) over days of a less b, per-day rates in points."""
    d = 100 * (a - b).dropna().sort_index()
    half = len(d) // 2
    bar = 1.96 * d.std(ddof=1) / len(d) ** 0.5 if len(d) > 1 else float('inf')
    return d.mean(), bar, d.iloc[:half].mean(), d.iloc[half:].mean()


def balanced(x: pd.DataFrame, v: str) -> float:
    """The hit rate on the bigger moves and on the smaller ones, averaged (NaN with only one kind)."""
    y, hit = x['bigger'].astype(bool), x[f'hit_{v}'].astype(bool)
    return (hit[y].mean() + hit[~y].mean()) / 2 if y.any() and (~y).any() else float('nan')


def per_day(x: pd.DataFrame, fn) -> pd.Series:
    return pd.Series({d: fn(g) for d, g in x.groupby('day')}, dtype=float)


def rank_corr(x: pd.DataFrame, v: str) -> pd.Series:
    """Per day, the rank correlation of GEX at price with the move being bigger (negative is
    the right way round: less GEX, bigger moves)."""
    return per_day(x, lambda g: g[f'g_{v}'].rank().corr(g['bigger'].astype(float)))


def holds(edge: tuple) -> bool:
    mean, bar, first, later = edge
    return abs(mean) > bar and first * later > 0


def show(e: tuple) -> str:
    return f'{e[0]:+5.1f} ±{e[1]:4.1f} ({e[2]:+5.1f} / {e[3]:+5.1f})'


def report_size(s: pd.DataFrame, passed: list) -> int:
    print("\n== size: GEX at price, the scoreboard's moments. Plain hit rate, balanced accuracy, then each against oi per day (first / later half)")
    print(f'  {"min":>3s} {"n":>5s} {"bigger":>6s} | hit {"oi":>4s} {"signed":>6s} {"flow":>5s} | balanced {"oi":>4s} {"signed":>6s} {"flow":>5s} | {"signed - oi":28s} flow - oi')
    tests = 0
    for h in HORIZONS:
        x = s[s['h'] == h]
        bal = {v: per_day(x, lambda g, v=v: balanced(g, v)) for v in VARIANTS}
        a, b = day_edge(bal['signed'], bal['oi']), day_edge(bal['flow'], bal['oi'])
        hit = [100 * x[f'hit_{v}'].mean() for v in VARIANTS]
        print(f'  {h:3d} {len(x):5d} {100 * x["bigger"].mean():5.0f}% |     {hit[0]:3.0f}% {hit[1]:5.0f}% {hit[2]:4.0f}% |'
              f'          {100 * balanced(x, "oi"):3.0f}% {100 * balanced(x, "signed"):5.0f}% {100 * balanced(x, "flow"):4.0f}% | {show(a)}   {show(b)}')
        for name, e in (('signed', a), ('flow', b)):
            tests += 1
            if holds(e) and e[0] > 0:
                passed.append(f'size: {name} beats oi at {h}m')
    print('  within a day, GEX at price vs a bigger move (rank correlation, negative = right way, 95% bar over days):')
    for h in HORIZONS:
        x = s[s['h'] == h]
        cells = []
        for v in VARIANTS:
            r = rank_corr(x, v).dropna()
            cells.append(f'{v} {r.mean():+.2f} ±{1.96 * r.std(ddof=1) / len(r) ** 0.5:.2f}')
        print(f'  {h:3d}  ' + '   '.join(cells))
    x = s.drop_duplicates(['day', 'm'])
    print(f'  customers had net sold gamma at the price at {100 * (x["g_flow"] > 0).mean():.0f}% of moments; '
          f'median |GEX at price| {x["g_oi"].abs().median() / 1e6:.0f}M, today\'s trades moved it a median {(x["g_signed"] - x["g_oi"]).abs().median() / 1e6:.0f}M')
    for v in ('signed', 'flow'):
        flip = s[(s[f'g_{v}'] < 0) != (s['g_oi'] < 0)]
        print(f'  {v} has the opposite sign to oi at {100 * ((x[f"g_{v}"] < 0) != (x["g_oi"] < 0)).mean():.0f}% of moments; '
              f'there {v} right {100 * flip[f"hit_{v}"].mean() if len(flip) else 0:.0f}%, oi {100 * flip["hit_oi"].mean() if len(flip) else 0:.0f}% (n {len(flip)})')
    return tests


def report_charm(c: pd.DataFrame, passed: list) -> int:
    print('\n== charm: dealers\' 0DTE charm flow at price, BUY -> SPX up. Hit rate less a coin per day (first / later half)')
    x = c.drop_duplicates(['day', 'm'])
    print('  BUY at: ' + ', '.join(f'{v} {100 * (x[f"flow_{v}"] > 0).mean():.0f}%' for v in VARIANTS) + f' of {len(x)} checkpoints')
    print(f'  {"to":>5s} {"n":>5s} {"oi":28s} {"signed":28s} flow')
    tests = 0
    for h in (*(str(h) for h in HORIZONS), 'close'):
        y = c[c['h'] == h]
        a, b, d = (day_edge(y.groupby('day')[f'hit_{v}'].mean()) for v in VARIANTS)
        print(f'  {h:>5s} {len(y):5d} {show(a)}   {show(b)}   {show(d)}')
        for name, e in (('signed', b), ('flow', d)):
            tests += 1
            if holds(e):
                passed.append(f'charm: {name} {"with" if e[0] > 0 else "against"} its sign, to {h}')
    return tests


def level_edge(r: pd.DataFrame, f: pd.DataFrame) -> tuple:
    """(level less its fakes at 30 min, 95% bar, first half of the days, later half)"""
    edge, bar = L.vs_fake(r, f)
    days = sorted(r['day'].unique())
    cut = days[len(days) // 2]
    return edge, bar, L.vs_fake(r[r['day'] < cut], f[f['day'] < cut])[0], L.vs_fake(r[r['day'] >= cut], f[f['day'] >= cut])[0]


def report_levels(lv: pd.DataFrame, passed: list) -> int:
    print('\n== levels built from each reading, against fake levels. vs fake = pts further through at 30 min, 95% bar')
    tests = 0
    for v in VARIANTS:
        d = lv[lv['variant'] == v]
        real, fake = d[d['real']], d[~d['real']]
        print(f'  {v}: {len(real)} real approaches, {len(fake)} fake')
        for fam in FAMILIES:
            r, f = real[real['level'] == fam], fake[fake['level'] == fam]
            print('  ' + L.line(fam, r, f))
            if v != 'oi' and len(r) >= 12 and len(f) >= 12:
                tests += 1
                e = level_edge(r, f)
                if holds(e):
                    passed.append(f'levels: {v} {fam} ({"more through" if e[0] > 0 else "more bounce"} than its fakes)')
    return tests


def main():
    by_day = P.sessions(max_age=0)
    now = datetime.now(cm.ET)
    days = sorted(os.path.basename(f)[len('option_ticks_SPX_'):][:10]
                  for f in glob.glob(os.path.join(gex_stats._DATA_DIR, 'option_ticks_SPX_2*')))
    days = [d for d in days if len(by_day.get(d, ())) >= SIZE_DAYS and (d != now.date().isoformat() or now.time() >= dtime(16, 15))]
    all_days = sorted(by_day)
    rngs = {v: random.Random(7) for v in VARIANTS}
    size, charm, levels = [], [], []
    done, classed, skipped = [], 0, 0
    for day in days:
        heat = gex_stats.load_heatmap('SPX', day=date.fromisoformat(day))
        bars_path = gex_stats.day_file(f'option_bars_SPX_{day}.csv')
        if not heat.get('times') or not bars_path:
            print(day, 'no heatmap or bars', flush=True)
            continue
        tr = tf.trades(day)
        keep = (tr['s'] != 0) & ~tr['outside']
        classed += tr.loc[keep, 'dv'].sum()
        skipped += tr.loc[~keep, 'dv'].sum()
        flows, book, bars = net_bought(tr), load_bars(day), by_day[day]
        cols = variant_columns(heat, flows, book)
        tl = {v: P.level_timeline({**heat, 'all': cols[v]}) for v in VARIANTS}
        size += size_rows(day, bars, P.baselines(day, {d: by_day[d] for d in all_days}), tl)
        charm += charm_rows(day, bars, flows, book)
        levels += level_rows(day, bars, tl, rngs)
        done.append(day)
        print(day, len(size), len(charm), len(levels), flush=True)
    print(f'\n{len(done)} days {done[0]} .. {done[-1]}, before 15:30; {classed:,.0f} contracts classed bought or sold, '
          f'{skipped:,.0f} skipped ({100 * skipped / (classed + skipped):.0f}%: multi-leg prints, unclassified)')
    s, c, lv = pd.DataFrame(size), pd.DataFrame(charm), pd.DataFrame(levels)
    passed = []
    tests = report_size(s, passed) + report_charm(c, passed) + report_levels(lv, passed)
    print(f'\nRESULT: {tests} tests; about {0.05 * tests:.1f} by chance; passed: ' + (' | '.join(passed) or 'none'))
    out = os.path.join(os.environ.get('TEMP', '.'), 'signed_gex_rows.csv')
    s.to_csv(out, index=False)
    print('rows:', out)


if __name__ == '__main__':
    main()
