"""
Buy 0DTE SPX options when price reaches a level, not at random minutes: does what we
buy there gain value?

A touch: price within TOUCH pts of a level after being APPROACH+ pts away in the last 10
minutes (one per level per 15 minutes). Levels, each as it stood at that minute:
  gold / silver / bronze   the 3 biggest |GEX| strikes (heatmap snapshots; All and 0DTE)
  big above / big below    the nearest BIG strike above / below price (the chart's BIG lines)
  trapdoor, squeeze, support, resist   (gamma levels, All)
  0DTE flip / call wall / put wall / pin, call wall / put wall / flip / pin / true pin (ex-0DTE)
  open / prev close        the day's 9:30 price / the previous session's close

At each touch, three trades (0DTE, in at mid + half spread, out at mid - half spread):
  bounce   price came down to the level: ATM call; came up to it: ATM put
  break    the opposite: through the level
  gold     the contract struck AT the gold strike (All), toward it: a call if gold is
           above price, a put if below (skipped within 5 pts of it)
Out after 15 / 30 / 60 minutes or at 15:50; 'hit +25%' = it traded 25% above what was
paid at some point within 60 minutes (a take-profit there would have filled).

0DTE prices exist from 2026-10-13 (12 sessions by 10/28), so the option tables are small.
The last table uses SPX alone on every day the level exists (walls / flip / pin from
June, gold and gamma levels from 2026-10-06, open from May): did price bounce?

Run:  python tools/odte_level_test.py
"""
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import odte_edge_test as E  # noqa: E402  (loads SPX minutes; its main() doesn't run)
import gex_stats  # noqa: E402  (on the path via odte_edge_test)

TOUCH, APPROACH, LOOKBACK, COOLDOWN = 2.0, 5.0, 10, 15
HOLDS = (15, 30, 60)
BIG_BAND, BIG_MIN, BIG_SIDE = 250, 0.5, 4


def heat_levels(day, idx, spot):
    """Per minute: gold / silver / bronze (All and 0DTE) and the nearest BIG strike above /
    below, from the latest heatmap snapshot at or before that minute."""
    h = gex_stats.load_heatmap('SPX', day=date.fromisoformat(day))
    out = {k: np.full(len(idx), np.nan) for k in ('gold', 'silver', 'bronze', 'gold 0DTE', 'big above', 'big below')}
    if not h['times']:
        return out
    ks = np.array(h['strikes'])
    times = np.array(h['times'])
    pos = np.searchsorted(times, idx, side='right') - 1
    for i, p in enumerate(pos):
        if p < 0:
            continue
        g = np.array(h['all'][p], dtype=float)
        top = np.argsort(-np.abs(g))[:3]
        out['gold'][i], out['silver'][i], out['bronze'][i] = ks[top]
        out['gold 0DTE'][i] = ks[np.argmax(np.abs(np.array(h['odte'][p], dtype=float)))]
        near = np.abs(ks - spot[i]) <= BIG_BAND
        if near.any():
            mx = np.abs(g[near]).max()
            big = near & (np.abs(g) >= BIG_MIN * mx)
            up = [k for k in ks[big & (ks > spot[i])]][:]
            dn = [k for k in ks[big & (ks < spot[i])]]
            # the BIG_SIDE biggest a side, then the nearest of those
            up = sorted(up, key=lambda k: -abs(g[ks == k][0]))[:BIG_SIDE]
            dn = sorted(dn, key=lambda k: -abs(g[ks == k][0]))[:BIG_SIDE]
            if up:
                out['big above'][i] = min(up)
            if dn:
                out['big below'][i] = max(dn)
    return out


def asof_series(frame, col, idx):
    if frame is None or frame.empty or col not in frame:
        return np.full(len(idx), np.nan)
    f = frame[['t', col]].dropna().sort_values('t')
    if f.empty:
        return np.full(len(idx), np.nan)
    m = pd.merge_asof(pd.DataFrame({'t': idx}), f, on='t', direction='backward', tolerance=600)
    return pd.to_numeric(m[col], errors='coerce').values


def day_events(day):
    s = E.spx[E.spx.day == day].set_index('t')['close']
    if len(s) < 300:
        return []
    t0 = int(s.index.min())
    idx = np.arange(t0, t0 + 390 * 60, 60)
    spot = s.reindex(idx).ffill().values
    hm = 570 + (idx - t0) // 60
    exit_j = np.where(hm == E.EXIT_BY)[0]
    exit_j = exit_j[0] if len(exit_j) else None

    levels = heat_levels(day, idx, spot)
    gl = E.gamma_history(day)
    for k, col in (('trapdoor', 'trapdoor'), ('squeeze', 'squeeze'), ('support', 'support'), ('resist', 'resistance')):
        levels[k] = asof_series(gl, col, idx)
    o = E.snapshots('gex_0dte_snapshots_SPX', day, ['flip_level', 'call_wall', 'put_wall', 'pin'])
    for k, col in (('0DTE flip', 'flip_level'), ('0DTE call wall', 'call_wall'), ('0DTE put wall', 'put_wall'), ('0DTE pin', 'pin')):
        levels[k] = asof_series(o, col, idx)
    a = E.snapshots('gex_snapshots_SPX', day, ['flip_level', 'call_wall', 'put_wall', 'pin', 'true_pin'])
    for k, col in (('call wall', 'call_wall'), ('put wall', 'put_wall'), ('flip', 'flip_level'), ('pin', 'pin'), ('true pin', 'true_pin')):
        levels[k] = asof_series(a, col, idx)
    levels['open'] = np.full(len(idx), spot[0])
    pc = E.prev_close.get(day)
    levels['prev close'] = np.full(len(idx), pc if pc and pc > 0 else np.nan)

    piv = None
    p = E.day_file('option_trade_bars_SPX', day)
    if p:
        ob = pd.read_csv(p, usecols=['time', 'expiry', 'strike', 'side', 'close'])
        ob = ob[ob.expiry == day]
        if not ob.empty:
            piv = ob.pivot_table(index='time', columns=['side', 'strike'], values='close', aggfunc='last').reindex(idx).ffill(limit=2)

    def trade(side, k, i):
        """P&L % at each hold and to 15:50, and the best price within 60 min vs paid."""
        if piv is None or (side, k) not in piv:
            return None
        px = piv[(side, k)].values
        if not px[i] > 0:
            return None
        r = {}
        for h in HOLDS:
            j = i + h
            if j < len(idx) and hm[j] <= E.EXIT_BY and px[j] > 0:
                r[h] = E.pct(px[i], px[j])
        if exit_j and exit_j > i and px[exit_j] > 0:
            r['close'] = E.pct(px[i], px[exit_j])
        paid = px[i] + E.spread(px[i]) / 2
        w = px[i + 1:min(i + 61, len(idx))]
        w = w[w > 0]
        r['hit25'] = float(len(w) > 0 and (w - E.spread(w) / 2).max() >= 1.25 * paid)
        return r

    events = []
    # baseline: the same trades every 15 minutes at no level, 'from above' = fell over the
    # last 10 minutes (bounce = reversal, break = continuation)
    levels['(no level, every 15 min)'] = np.full(len(idx), np.nan)
    for name, L in levels.items():
        last = -10**9
        base = name.startswith('(no level')
        for i in range(LOOKBACK, len(idx)):
            if not (E.OPEN <= hm[i] <= E.LAST_ENTRY):
                continue
            if base:
                if (hm[i] - E.OPEN) % 15 or spot[i - LOOKBACK] == spot[i]:
                    continue
                from_above = spot[i - LOOKBACK] > spot[i]
            else:
                if np.isnan(L[i]) or abs(spot[i] - L[i]) > TOUCH or i - last < COOLDOWN:
                    continue
                back = spot[i - LOOKBACK:i] - L[i]
                far = np.where(np.abs(back) >= APPROACH)[0]
                if not len(far):
                    continue
                last = i
                from_above = back[far[-1]] > 0                  # where it was before coming in
            ev = {'day': day, 'level': name, 'hm': hm[i], 'from': 'above' if from_above else 'below'}
            for h in HOLDS:
                j = i + h
                if j < len(idx) and hm[j] <= E.EXIT_BY:
                    move = 1e4 * np.log(spot[j] / spot[i])
                    ev[f'bounce_spx_{h}'] = move if from_above else -move
            k = 5 * round(spot[i] / 5)
            for kind, side in (('bounce', 'call' if from_above else 'put'), ('break', 'put' if from_above else 'call')):
                r = trade(side, k, i)
                if r:
                    ev.update({f'{kind}_{h}': v for h, v in r.items()})
            g = levels['gold'][i]
            if not np.isnan(g) and abs(g - spot[i]) >= 5:
                r = trade('call' if g > spot[i] else 'put', g, i)
                if r:
                    ev.update({f'gold_{h}': v for h, v in r.items()})
            events.append(ev)
    return events


def main():
    pd.set_option('display.width', 220)
    ev = pd.DataFrame([e for d in sorted(E.spx.day.unique()) for e in day_events(d)])
    opt = ev[ev['bounce_30'].notna() | ev['break_30'].notna()]
    print(f'{len(ev):,} level touches over {ev.day.nunique()} sessions; {len(opt):,} on the {opt.day.nunique()} '
          f'sessions with 0DTE prices ({opt.day.min()} .. {opt.day.max()})')

    order = list(dict.fromkeys(['gold', 'silver', 'bronze', 'gold 0DTE', 'big above', 'big below', 'trapdoor', 'squeeze',
                                'support', 'resist', '0DTE flip', '0DTE call wall', '0DTE put wall', '0DTE pin',
                                'call wall', 'put wall', 'flip', 'pin', 'true pin', 'open', 'prev close', '(no level, every 15 min)']))
    rows = []
    for lv in order:
        g = opt[opt.level == lv]
        if len(g) < 5:
            continue
        r = {'level': lv, 'touches': len(g), 'days': g.day.nunique()}
        for kind in ('bounce', 'break', 'gold'):
            for h in (15, 30, 60, 'close'):
                c = f'{kind}_{h}'
                if c in g and g[c].notna().sum() >= 5:
                    r[f'{kind} {h}'] = g[c].mean()
            c = f'{kind}_hit25'
            if c in g and g[c].notna().sum() >= 5:
                r[f'{kind} hit+25%'] = 100 * g[c].mean()
        rows.append(r)
    t = pd.DataFrame(rows)
    fmt = lambda v: f'{v:,.1f}'
    print('\nBUYING AT A TOUCH: average P&L % of premium after spread (0DTE days only)')
    for kind in ('bounce', 'break', 'gold'):
        cols = ['level', 'touches', 'days'] + [c for c in t.columns if c.startswith(kind + ' ')]
        print(f'\n  {kind.upper()}' + {'bounce': ' (came down to it: call; came up to it: put)',
                                        'break': ' (through it)',
                                        'gold': ' (the gold-strike contract, toward gold)'}[kind])
        print(t[cols].to_string(index=False, float_format=fmt))

    # every level's touches together, and split by time of day
    rows = []
    base = opt[opt.level.str.startswith('(no level')]
    lv = opt[~opt.level.str.startswith('(no level')]
    for lab, g in (('all level touches', lv), ('  before 12:00', lv[lv.hm < 720]), ('  12:00-14:00', lv[(lv.hm >= 720) & (lv.hm < 840)]),
                   ('  after 14:00', lv[lv.hm >= 840]), ('no level (baseline)', base), ('  before 12:00', base[base.hm < 720]),
                   ('  12:00-14:00', base[(base.hm >= 720) & (base.hm < 840)]), ('  after 14:00', base[base.hm >= 840])):
        r = {'touches': lab, 'n': len(g)}
        for kind in ('bounce', 'break', 'gold'):
            r[f'{kind} 30'] = g[f'{kind}_30'].mean()
            r[f'{kind} hit+25%'] = 100 * g[f'{kind}_hit25'].mean()
        rows.append(r)
    print('\nALL LEVELS TOGETHER')
    print(pd.DataFrame(rows).to_string(index=False, float_format=fmt))

    # SPX alone over every day the level exists: did it bounce?
    rows = []
    for lv in order:
        g = ev[ev.level == lv]
        if len(g) < 10:
            continue
        dm = g.groupby('day')['bounce_spx_30'].mean().dropna()
        tstat = dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm))) if len(dm) > 2 and dm.std() > 0 else np.nan
        rows.append({'level': lv, 'touches': len(g), 'days': g.day.nunique(),
                     **{f'bounced at {h}m %': 100 * (g[f'bounce_spx_{h}'] > 0).mean() for h in HOLDS},
                     'avg bounce 30m (bps)': g['bounce_spx_30'].mean(), 't (days)': tstat})
    print('\nSPX ALONE, EVERY DAY WITH THE LEVEL: % of touches where price moved back away (bounce) after 15 / 30 / 60 min')
    print(pd.DataFrame(rows).to_string(index=False, float_format=fmt))


if __name__ == '__main__':
    main()
