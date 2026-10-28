"""
Buy 0DTE SPX options when price reaches a level, not at random minutes: does what we
buy there gain value?

A touch: price within TOUCH pts of a level after being APPROACH+ pts away in the last 10
minutes (one per level per 15 minutes). Levels, each as it stood at that minute:
  gold / silver / bronze   the 3 biggest |GEX| strikes (heatmap snapshots; All and 0DTE)
  key above / key below    the nearest key strike above / below price (the chart's KEY STRIKES)
  trapdoor, squeeze, support, resist   (gamma levels, All)
  0DTE flip / call wall / put wall / pin, call wall / put wall / flip / pin / true pin (ex-0DTE)
  open / prev open / prev close   the day's 9:30 price, and the previous session's
                                  9:30 price and close

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
ZONES = (2.0, 5.0, 8.0)          # near the level, not only on it: within this many pts, after
                                 # being zone + APPROACH away in the last LOOKBACK minutes
HOLDS = (15, 30, 60)
KEY_BAND, KEY_MIN, KEY_SIDE = 250, 0.5, 4


PRICE_LEVELS = ('open', 'prev open', 'prev close')
GEX_LEVELS = ('gold', 'silver', 'bronze', 'gold 0DTE', 'key above', 'key below', 'trapdoor', 'squeeze', 'support',
              'resist', '0DTE flip', '0DTE call wall', '0DTE put wall', '0DTE pin', 'call wall', 'put wall', 'flip',
              'pin', 'true pin')
NEAR_PTS = 5.0                   # a price level 'with a GEX level' when one is this close
_first = E.spx.groupby('day')['close'].first()
PREV_OPEN = _first.shift(1)      # the previous session's 9:30 price


def heat_levels(day, idx, spot):
    """Per minute: gold / silver / bronze (All and 0DTE) and the nearest key strike above /
    below, from the latest heatmap snapshot at or before that minute."""
    h = gex_stats.load_heatmap('SPX', day=date.fromisoformat(day))
    out = {k: np.full(len(idx), np.nan) for k in ('gold', 'silver', 'bronze', 'gold 0DTE', 'key above', 'key below')}
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
        near = np.abs(ks - spot[i]) <= KEY_BAND
        if near.any():
            mx = np.abs(g[near]).max()
            key = near & (np.abs(g) >= KEY_MIN * mx)
            up = [k for k in ks[key & (ks > spot[i])]][:]
            dn = [k for k in ks[key & (ks < spot[i])]]
            # the KEY_SIDE biggest a side, then the nearest of those
            up = sorted(up, key=lambda k: -abs(g[ks == k][0]))[:KEY_SIDE]
            dn = sorted(dn, key=lambda k: -abs(g[ks == k][0]))[:KEY_SIDE]
            if up:
                out['key above'][i] = min(up)
            if dn:
                out['key below'][i] = max(dn)
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
        return [], {'day': day}
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
    pc, po = E.prev_close.get(day), PREV_OPEN.get(day)
    levels['prev close'] = np.full(len(idx), pc if pc and pc > 0 else np.nan)
    levels['prev open'] = np.full(len(idx), po if po and po > 0 else np.nan)

    def nearest_gex(i, price):
        """(distance, name) of the GEX level nearest `price` at minute i."""
        best = (np.nan, None)
        for n in GEX_LEVELS:
            v = levels[n][i]
            if not np.isnan(v) and not abs(v - price) >= best[0]:
                best = (abs(v - price), n)
        return best

    # how close the open / previous open / previous close sit to the GEX levels at 9:35
    i0 = int(np.where(hm >= E.OPEN)[0][0])
    info = {'day': day}
    for n in PRICE_LEVELS:
        v = levels[n][i0]
        if not np.isnan(v):
            info[n], info[n + ' near'] = nearest_gex(i0, v)

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
        got = w - E.spread(w) / 2                          # what selling each minute would fetch
        r['hit25'] = float(len(w) > 0 and got.max() >= 1.25 * paid)
        if len(w):
            r['best60'] = 100 * (got.max() - paid) / paid
            # take profit: out the first minute it's worth +25%, else at 60 min (or the last quote)
            hit = np.where(got >= 1.25 * paid)[0]
            r['tp25'] = 100 * ((got[hit[0]] if len(hit) else got[-1]) - paid) / paid
        return r

    events = []
    # baseline: the same trades every 15 minutes at no level, 'from above' = fell over the
    # last 10 minutes (bounce = reversal, break = continuation)
    levels['(no level, every 15 min)'] = np.full(len(idx), np.nan)
    for name, L, zone in [(n, L, z) for n, L in levels.items() for z in ((0,) if n.startswith('(no level') else ZONES)]:
        last = -10**9
        base = zone == 0
        for i in range(LOOKBACK, len(idx)):
            if not (E.OPEN <= hm[i] <= E.LAST_ENTRY):
                continue
            if base:
                if (hm[i] - E.OPEN) % 15 or spot[i - LOOKBACK] == spot[i]:
                    continue
                from_above = spot[i - LOOKBACK] > spot[i]
            else:
                if np.isnan(L[i]) or abs(spot[i] - L[i]) > zone or i - last < COOLDOWN:
                    continue
                back = spot[i - LOOKBACK:i] - L[i]
                far = np.where(np.abs(back) >= zone + APPROACH)[0]
                if not len(far):
                    continue
                last = i
                from_above = back[far[-1]] > 0                  # where it was before coming in
            ev = {'day': day, 'level': name, 'zone': zone, 'hm': hm[i], 'from': 'above' if from_above else 'below'}
            run = (spot[i + 1:i + 31] - spot[i]) * (1 if from_above else -1)   # points in the bounce direction
            if len(run) >= 25:
                ev['ran5'], ev['ran10'], ev['against'] = float(run.max() >= 5), float(run.max() >= 10), -run.min()
                # which came first within 30 min: 5 pts the bounce way, or 5 pts through
                up, dn = np.where(run >= 5)[0], np.where(run <= -5)[0]
                if len(up) or len(dn):
                    ev['first5'] = float(len(up) > 0 and (not len(dn) or up[0] < dn[0]))
            if name in PRICE_LEVELS:                          # a GEX level at the same price?
                ev['gex_dist'], ev['gex_near'] = nearest_gex(i, L[i])
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
    return events, info


def main():
    pd.set_option('display.width', 220)
    out = [day_events(d) for d in sorted(E.spx.day.unique())]
    allev = pd.DataFrame([e for evs, _ in out for e in evs])
    ev = allev[allev.zone.isin([TOUCH, 0])]
    infos = pd.DataFrame([info for _, info in out if len(info) > 1])
    opt = ev[ev['bounce_30'].notna() | ev['break_30'].notna()]
    print(f'{len(ev):,} level touches over {ev.day.nunique()} sessions; {len(opt):,} on the {opt.day.nunique()} '
          f'sessions with 0DTE prices ({opt.day.min()} .. {opt.day.max()})')

    order = list(dict.fromkeys(['gold', 'silver', 'bronze', 'gold 0DTE', 'key above', 'key below', 'trapdoor', 'squeeze',
                                'support', 'resist', '0DTE flip', '0DTE call wall', '0DTE put wall', '0DTE pin',
                                'call wall', 'put wall', 'flip', 'pin', 'true pin', 'open', 'prev open', 'prev close',
                                '(no level, every 15 min)']))
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
    zones_report(allev)
    price_levels_report(allev[allev.zone.isin([5.0, 0])], infos)



def zones_report(allev):
    """The bounce near each level, not only on it: did price run in the bounce direction
    within 30 min (SPX, every session), and what the 0DTE bounce made with a take-profit
    at +25% (else out at 60 min; the 12 sessions with prices)."""
    def row(label, zone, g):
        o = g[g['bounce_tp25'].notna()] if 'bounce_tp25' in g else g.iloc[0:0]
        few = len(o) < 5
        return {'level': label, 'zone pts': zone, 'touches': len(g), 'days': g.day.nunique(),
                '5 pts bounce way first %': 100 * g['first5'].mean(), 'ran 5+ pts %': 100 * g['ran5'].mean(),
                'ran 10+ pts %': 100 * g['ran10'].mean(), 'median pts against': g['against'].median(),
                'option trades': len(o),
                'TP +25% rule %': np.nan if few else o['bounce_tp25'].mean(),
                'hit +25% %': np.nan if few else 100 * o['bounce_hit25'].mean(),
                'median best %': np.nan if few else o['bounce_best60'].median()}
    rows = [row(lv, z, g) for lv in GEX_LEVELS + PRICE_LEVELS for z in ZONES
            for g in [allev[(allev.level == lv) & (allev.zone == z)]] if len(g) >= 10]
    rows.append(row('(no level, every 15 min)', '-', allev[allev.zone == 0]))
    print('\nNEAR THE LEVEL, NOT ONLY ON IT: the bounce within 2 / 5 / 8 pts of each level. Did SPX run 5+ / 10+ pts '
          'the bounce way within 30 min, and the 0DTE bounce with a take-profit at +25% (else out at 60 min)')
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f'{v:,.1f}'))


def price_levels_report(ev, infos):
    """Open / previous open / previous close: how near the GEX levels they sit, and do
    they bounce more when one is there."""
    print('\nOPEN, PREVIOUS OPEN, PREVIOUS CLOSE vs THE GEX LEVELS (at 9:35; GEX levels exist on '
          f'{infos[[c for c in infos if c.endswith(" near")]].notna().any(axis=1).sum()} of {len(infos)} sessions)')
    rows = []
    for n in PRICE_LEVELS:
        d = infos[[n, n + ' near']].dropna() if n in infos else pd.DataFrame()
        if d.empty:
            continue
        top = d[n + ' near'][d[n] <= NEAR_PTS].value_counts().head(3)
        rows.append({'price': n, 'sessions': len(d), 'median pts to nearest GEX level': d[n].median(),
                     'within 3 pts %': 100 * (d[n] <= 3).mean(), f'within {NEAR_PTS:g} pts %': 100 * (d[n] <= NEAR_PTS).mean(),
                     'within 10 pts %': 100 * (d[n] <= 10).mean(),
                     f'which (within {NEAR_PTS:g})': ', '.join(f'{k} {v}' for k, v in top.items())})
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f'{v:,.1f}'))

    rows = []
    for n in PRICE_LEVELS:
        g = ev[ev.level == n]
        for lab, sub in ((f'with a GEX level within {NEAR_PTS:g} pts', g[g.gex_dist <= NEAR_PTS]),
                         ('no GEX level near', g[~(g.gex_dist <= NEAR_PTS)])):
            if len(sub) < 5:
                continue
            o = sub['bounce_30'].dropna() if 'bounce_30' in sub else pd.Series(dtype=float)
            rows.append({'price': n, 'touch': lab, 'touches': len(sub), 'days': sub.day.nunique(),
                         '5 pts bounce way first %': 100 * sub['first5'].mean(),
                         **{f'bounced {h}m %': 100 * (sub[f'bounce_spx_{h}'] > 0).mean() for h in HOLDS},
                         'option bounce 30m %': o.mean() if len(o) >= 5 else np.nan, 'option n': len(o)})
    print('\n  touches: did price bounce off it (SPX, every session), and the 0DTE bounce trade (12 sessions)')
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f'{v:,.1f}'))


if __name__ == '__main__':
    main()
