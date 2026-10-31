"""
Can anything we record tell when a 0DTE SPX option bought now will be worth more later?

For every minute of every session with 0DTE prices (from 2026-10-13: Schwab drops an
expired contract's 1-minute bars, so only days the app pulled before the close), "buy"
the 0DTE contract nearest the money -- call, put, or both (straddle) -- and "sell" it h
minutes later, paying half the bid/ask spread each way (the mean spread for that option
price, measured from the stream's bid/ask bars). Direction tests use every session with
SPX minutes (from 2026-05-04). tools/odte_level_test.py buys at levels instead.

  A. no signal: what each trade makes on average, by hold and time of day
  B. one signal at a time: GEX levels and regime, momentum, range, implied vs realized
     move, internals, flow. Does any state make calls or puts pay in BOTH halves of the
     days (thresholds from all days: a screen, not a test)
  C. every signal together: LightGBM, walk-forward by day (each day predicted by a model
     trained only on the days before it)
  D. the stream's bid/ask days (from 2026-10-14): A at the real ask in and bid out, to
     check the spread model

Prices: the trade bars' last trade each minute (at the bid or the ask, so a mid on
average). Run with a Python that has lightgbm:   python tools/odte_edge_test.py
"""
import glob
import os
import sys
import tempfile

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, 'data')
sys.path.insert(0, os.path.join(ROOT, 'src'))
ET, PT = 'America/New_York', 'America/Los_Angeles'
HOLDS = (5, 15, 30, 60)
OPEN, LAST_ENTRY, EXIT_BY = 9 * 60 + 35, 15 * 60 + 30, 15 * 60 + 50   # ET minutes


def spread(mid):
    """Mean 0DTE near-the-money bid/ask spread by option price (option_bars, 10/14-10/28)."""
    mid = np.asarray(mid, dtype=float)
    return np.select([mid <= 1, mid <= 2, mid <= 4, mid <= 8, mid <= 15], [0.06, 0.065, 0.09, 0.13, 0.165], 0.20)


def pct(entry, exit_):
    """P&L in % of what was paid: in at mid + half spread, out at mid - half spread."""
    paid = entry + spread(entry) / 2
    return 100 * ((exit_ - spread(exit_) / 2) - paid) / paid


def day_file(prefix, day):
    p = sorted(glob.glob(os.path.join(D, f'{prefix}_{day}.csv*')))
    return p[0] if p else None


def snapshots(prefix, day, cols):
    """A day's gex snapshot CSV (local PT stamps) -> epoch-sorted frame with `cols`."""
    p = day_file(prefix, day)
    if not p:
        return None
    df = pd.read_csv(p)
    ts = pd.to_datetime(df['timestamp']).dt.tz_localize(PT, ambiguous='NaT', nonexistent='NaT')
    df['t'] = (ts - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(seconds=1)   # any resolution
    keep = [c for c in cols if c in df]
    df[keep] = df[keep].apply(pd.to_numeric, errors='coerce')        # blank cells in older files
    return df[['t'] + keep].dropna(subset=['t']).sort_values('t')


# ── SPX 1-minute ───────────────────────────────────────────────────────────────
spx = pd.read_csv(os.path.join(D, 'price_history_SPX.csv'))
spx['t'] = spx['datetime'] // 1000
_et = pd.to_datetime(spx['t'], unit='s', utc=True).dt.tz_convert(ET)
spx['day'] = _et.dt.strftime('%Y-%m-%d')
spx['hm'] = _et.dt.hour * 60 + _et.dt.minute
spx = spx[(spx.hm >= 570) & (spx.hm < 960)].drop_duplicates('t')
prev_close = spx.groupby('day')['close'].last().shift(1)

flow_path = os.path.join(tempfile.gettempdir(), 'tick_flow_minutes.csv')   # tools/tick_flow_test.py's cache
flow_all = pd.read_csv(flow_path) if os.path.exists(flow_path) else None


def gamma_history(day):
    """support / resist / trapdoor / squeeze per heatmap snapshot (gex_grid, from 2026-10-06)."""
    try:
        import gex_stats
        from datetime import date
        L = gex_stats.current_gamma_levels(None, 'all', day=date.fromisoformat(day))
    except Exception:
        return None
    if not L or not L.get('history'):
        return None
    return pd.DataFrame(L['history']).rename(columns={'time': 't'}).sort_values('t')


def internals(day):
    p = day_file('internals', day)
    if not p:
        return None
    df = pd.read_csv(p, usecols=['time', 'symbol', 'close'])
    w = df.pivot_table(index='time', columns='symbol', values='close', aggfunc='last').sort_index()
    out = pd.DataFrame(index=w.index)
    g = lambda s: w[s] if s in w else np.nan
    out['tick'] = g('$TICK')
    out['tick5'] = out['tick'].rolling(5, min_periods=1).mean()
    out['trin'] = g('$TRIN')
    out['vix_ch15'] = g('$VIX') - g('$VIX').shift(15)
    out['vix1d'] = g('$VIX1D')
    out['adv_dec'] = g('$ADVN') - g('$DECN')
    out['uvol_share'] = g('$UVOL') / (g('$UVOL') + g('$DVOL'))
    out['nq_r15'] = 1e4 * np.log(g('/NQ') / g('/NQ').shift(15))
    return out.reset_index().rename(columns={'time': 't'})


def build_day(day):
    """One row per entry minute: SPX forward moves and the features; the ATM 0DTE trades'
    P&L where that day's 0DTE has trade bars (Schwab drops a contract's bars once it
    expires, so only days recorded live: from 2026-10-13)."""
    s = spx[spx.day == day].set_index('t')['close']
    if len(s) < 300:
        return None
    p = day_file('option_trade_bars_SPX', day)
    o = pd.read_csv(p, usecols=['time', 'expiry', 'strike', 'side', 'close']) if p else None
    o = o[o.expiry == day] if o is not None else None
    t0 = int(s.index.min())
    idx = np.arange(t0, t0 + 390 * 60, 60)
    s = s.reindex(idx).ffill()
    piv = (o.pivot_table(index='time', columns=['side', 'strike'], values='close', aggfunc='last').reindex(idx).ffill(limit=2)
           if o is not None and not o.empty else None)
    hm = 570 + (idx - t0) // 60
    spot = s.values
    close_j = np.where(hm == EXIT_BY)[0]
    rows = []
    for i in np.where((hm >= OPEN) & (hm <= LAST_ENTRY))[0]:
        k = 5 * round(spot[i] / 5)
        r = {'day': day, 't': idx[i], 'hm': hm[i], 'spot': spot[i], 'k': k}
        c = pu = None
        if piv is not None and ('call', k) in piv and ('put', k) in piv:
            c, pu = piv[('call', k)].values, piv[('put', k)].values
            if c[i] > 0 and pu[i] > 0:
                r['call0'], r['put0'] = c[i], pu[i]
            else:
                c = pu = None
        for h in HOLDS + ('close',):
            j = (close_j[0] if len(close_j) else -1) if h == 'close' else i + h
            if j < 0 or j >= len(idx) or hm[j] > EXIT_BY or j <= i:
                continue
            r[f'spx_{h}'] = 1e4 * np.log(spot[j] / spot[i])
            if c is None:
                continue
            if c[j] > 0:
                r[f'call_{h}'] = pct(c[i], c[j])
            if pu[j] > 0:
                r[f'put_{h}'] = pct(pu[i], pu[j])
            if c[j] > 0 and pu[j] > 0:
                paid = c[i] + pu[i] + (spread(c[i]) + spread(pu[i])) / 2
                got = c[j] + pu[j] - (spread(c[j]) + spread(pu[j])) / 2
                r[f'strad_{h}'] = 100 * (got - paid) / paid
        rows.append(r)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    for c in ('call0', 'put0'):
        if c not in df:
            df[c] = np.nan

    # ── features, all known at the entry minute ──
    ss = pd.Series(spot, index=idx)
    lr = np.log(ss).diff()
    for w in (5, 15, 30, 60):
        df[f'r{w}'] = df.t.map(1e4 * np.log(ss / ss.shift(w)))
    df['r_open'] = 1e4 * np.log(df.spot / spot[0])
    pc = prev_close.get(day)
    df['gap'] = 1e4 * np.log(spot[0] / pc) if pc and pc > 0 else np.nan
    hi, lo = ss.cummax(), ss.cummin()
    df['range'] = df.t.map(1e4 * (hi - lo) / ss)
    df['pos_in_range'] = df.t.map((ss - lo) / (hi - lo).replace(0, np.nan))
    sd30 = lr.rolling(30, min_periods=20).std()
    df['rv30'] = df.t.map(1e4 * sd30 * np.sqrt(390))
    left = (960 - df.hm).clip(lower=1)
    df['implied_move'] = 1e4 * (df.call0 + df.put0) / df.spot            # straddle, bps of spot
    df['iv_rv'] = df.implied_move / (df.t.map(1e4 * sd30) * np.sqrt(left))  # options vs recent realized
    df['strad_cost'] = df.call0 + df.put0

    def asof(frame, cols, prefix=''):
        nonlocal df
        if frame is None or frame.empty:
            for c in cols:
                df[prefix + c] = np.nan
            return
        m = pd.merge_asof(df[['t']], frame[['t'] + [c for c in cols if c in frame]].rename(columns={c: prefix + c for c in cols}),
                          on='t', direction='backward', tolerance=600)
        for c in cols:
            df[prefix + c] = m[prefix + c].values if prefix + c in m else np.nan

    asof(snapshots('gex_0dte_snapshots_SPX', day, ['total_gex', 'flip_level', 'put_wall', 'call_wall', 'pin']),
         ['total_gex', 'flip_level', 'put_wall', 'call_wall', 'pin'], 'o_')
    asof(snapshots('gex_snapshots_SPX', day, ['total_gex', 'flip_level', 'put_wall', 'call_wall', 'pin', 'true_pin', 'charm_flow']),
         ['total_gex', 'flip_level', 'put_wall', 'call_wall', 'pin', 'true_pin', 'charm_flow'], 'a_')
    asof(gamma_history(day), ['support', 'resistance', 'trapdoor', 'squeeze'], 'g_')
    asof(internals(day), ['tick', 'tick5', 'trin', 'vix_ch15', 'vix1d', 'adv_dec', 'uvol_share', 'nq_r15'])
    if flow_all is not None:
        f = flow_all[flow_all.day == day].rename(columns={'minute': 't'}).sort_values('t')
        if not f.empty:
            f = f.assign(flow15=f['odte'].rolling(15, min_periods=5).sum(), flow15_near=f['odte_stable_near'].rolling(15, min_periods=5).sum())
        asof(f if not f.empty else None, ['flow15', 'flow15_near'])
    else:
        df['flow15'] = df['flow15_near'] = np.nan

    # levels as signed distances from price (points), so they mean the same every day
    for c in ('o_flip_level', 'o_pin', 'a_flip_level', 'a_pin', 'a_true_pin'):
        df['d_' + c] = df.spot - df[c]
    df['d_o_call_wall'] = df.o_call_wall - df.spot
    df['d_o_put_wall'] = df.spot - df.o_put_wall
    df['d_a_call_wall'] = df.a_call_wall - df.spot
    df['d_a_put_wall'] = df.spot - df.a_put_wall
    df['d_trapdoor'] = df.spot - df.g_trapdoor
    df['d_squeeze'] = df.g_squeeze - df.spot
    df['d_support'] = df.spot - df.g_support
    df['d_resist'] = df.g_resistance - df.spot
    df['o_gex_pos'] = np.sign(df.o_total_gex)
    df['a_gex_pos'] = np.sign(df.a_total_gex)
    return df


FEATURES = ['hm', 'r5', 'r15', 'r30', 'r60', 'r_open', 'gap', 'range', 'pos_in_range', 'rv30', 'implied_move', 'iv_rv',
            'strad_cost', 'o_total_gex', 'a_total_gex', 'o_gex_pos', 'a_gex_pos', 'a_charm_flow',
            'd_o_flip_level', 'd_o_pin', 'd_o_call_wall', 'd_o_put_wall', 'd_a_flip_level', 'd_a_pin', 'd_a_true_pin',
            'd_a_call_wall', 'd_a_put_wall', 'd_trapdoor', 'd_squeeze', 'd_support', 'd_resist',
            'tick', 'tick5', 'trin', 'vix_ch15', 'vix1d', 'adv_dec', 'uvol_share', 'nq_r15', 'flow15', 'flow15_near']


def day_stats(df, col):
    """mean %, win %, days positive, t over day means (minutes overlap; days are the sample)."""
    x = df[['day', col]].dropna()
    if len(x) < 50:
        return None
    dm = x.groupby('day')[col].mean()
    t = dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm))) if len(dm) > 2 and dm.std() > 0 else np.nan
    return {'mean': x[col].mean(), 'median': x[col].median(), 'win': 100 * (x[col] > 0).mean(),
            'days+': f'{(dm > 0).sum()}/{len(dm)}', 't': t, 'n': len(x)}


def show(title, rows):
    print(f'\n{title}')
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f'{v:,.2f}'))


def walk_forward(df, target, min_train, refit, features):
    """LightGBM up/down classifier, each day predicted by a model of the days before it."""
    import lightgbm as lgb
    order = sorted(df.day.unique())
    preds, model = [], None
    for i, d in enumerate(order):
        if i < min_train:
            continue
        if model is None or (i - min_train) % refit == 0:
            tr = df[df.day.isin(order[:i])].dropna(subset=[target])
            model = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.03, num_leaves=15, min_child_samples=60,
                                       subsample=0.8, subsample_freq=1, colsample_bytree=0.8, verbose=-1)
            model.fit(tr[features], tr[target] > 0)
            lo_q, hi_q = np.quantile(model.predict_proba(tr[features])[:, 1], [0.2, 0.8])
        te = df[df.day == d].copy()
        te['p'] = model.predict_proba(te[features])[:, 1]
        te['lo_q'], te['hi_q'] = lo_q, hi_q
        preds.append(te)
    return pd.concat(preds) if preds else None


def screen(df, targets, min_days):
    """Each signal split in thirds (cut points from all days: a screen, not a test): the
    target's mean per third, overall and in each half of the days."""
    order = sorted(df.day.unique())
    first = df.day.map({d: i < len(order) // 2 for i, d in enumerate(order)})
    out = []
    for f in FEATURES:
        ok = df[f].notna()
        x = df[ok]
        if x.day.nunique() < min_days or x[f].nunique() < 2:
            continue
        bins = np.digitize(x[f], np.unique(np.nanquantile(x[f], [1 / 3, 2 / 3])))
        for b in np.unique(bins):
            g = x[bins == b]
            fh = first[ok][bins == b]
            for col in targets:
                st = day_stats(g, col)
                if st:
                    out.append({'signal': f, 'range': f'{g[f].min():,.4g}..{g[f].max():,.4g}', 'target': col,
                                'mean': st['mean'], 'up %': 100 * (g[col] > 0).mean(),
                                'first half': g[fh][col].mean(), 'second half': g[~fh][col].mean(),
                                'days+': st['days+'], 't': st['t'], 'days': g.day.nunique()})
    return pd.DataFrame(out)


def main():
    frames = [f for f in (build_day(d) for d in sorted(spx.day.unique())) if f is not None]
    allr = pd.concat(frames, ignore_index=True)
    allr[FEATURES] = allr[FEATURES].apply(pd.to_numeric, errors='coerce')   # None levels -> NaN
    a = allr[allr.call0.notna()].copy()                   # the days with 0DTE prices
    nd = a.day.nunique()
    print(f'OPTION PRICES: {nd} sessions {a.day.min()} .. {a.day.max()}, {len(a):,} entry minutes '
          f'(ATM 0DTE, entries {OPEN // 60}:{OPEN % 60:02d}-{LAST_ENTRY // 60}:{LAST_ENTRY % 60:02d} ET)')
    print(f'median ATM call {a.call0.median():.2f}  put {a.put0.median():.2f}  straddle {a.strad_cost.median():.2f}')

    # ── A. no signal ──
    rows = []
    for h in HOLDS + ('close',):
        for leg in ('call', 'put', 'strad'):
            st = day_stats(a, f'{leg}_{h}')
            if st:
                rows.append({'hold': h, 'buy': leg, **st})
    show('A. BUY AT THE MONEY, SELL LATER, no signal (P&L % of premium, after spread)', rows)
    a['bucket'] = pd.cut(a.hm, [0, 630, 720, 840, 900, 960], labels=['9:35-10:30', '10:30-12', '12-14', '14-15', '15-15:30'])
    rows = []
    for b, g in a.groupby('bucket', observed=True):
        for leg in ('call', 'put', 'strad'):
            st = day_stats(g, f'{leg}_30')
            if st:
                rows.append({'entry': b, 'buy': leg, 'hold': 30, **{k: st[k] for k in ('mean', 'win', 'days+', 't')}})
    show('A2. by entry time, 30-minute hold', rows)

    # what a direction call is worth: P&L when SPX went the option's way vs not
    rows = []
    for h in (15, 30, 60):
        for leg, up in (('call', True), ('put', False)):
            x = a[[f'{leg}_{h}', f'spx_{h}']].dropna()
            w = x[(x[f'spx_{h}'] > 0) == up][f'{leg}_{h}'].mean()
            l = x[(x[f'spx_{h}'] > 0) != up][f'{leg}_{h}'].mean()
            rows.append({'hold': h, 'buy': leg, 'avg when right %': w, 'avg when wrong %': l,
                         'right needed to break even %': 100 * -l / (w - l)})
    show("A3. WHAT DIRECTION IS WORTH: average P&L when SPX went the option's way vs the other way", rows)

    # ── B. one signal at a time, option P&L ──
    fb = screen(a, [f'{leg}_{h}' for leg in ('call', 'put', 'strad') for h in (15, 30)], min_days=8)
    both = fb[(fb['first half'] > 0) & (fb['second half'] > 0)]
    print(f'\nB. ONE SIGNAL AT A TIME (option P&L, {nd} days): {len(fb)} signal-state x trade cells; '
          f'{len(both)} made money in both halves of the days; {(both.t > 2).sum()} of those with t > 2 over days')
    if len(both):
        show('   best 12 by their worse half',
             both.assign(worst=both[['first half', 'second half']].min(axis=1)).sort_values('worst', ascending=False)
             .head(12).drop(columns=['worst', 'up %']).to_dict('records'))

    # ── E. direction over the long history (SPX moves drive call / put P&L) ──
    nall = allr.day.nunique()
    print(f'\nDIRECTION: {nall} sessions of SPX minutes {allr.day.min()} .. {allr.day.max()} '
          f'({allr[allr.o_total_gex.notna()].day.nunique()} with GEX snapshots, '
          f'{allr[allr.g_trapdoor.notna()].day.nunique()} with gamma levels, '
          f'{allr[allr.tick.notna()].day.nunique()} with internals)')
    fe = screen(allr, ['spx_15', 'spx_30', 'spx_60'], min_days=10)
    same = fe[np.sign(fe['first half']) == np.sign(fe['second half'])]
    strong = same[same.t.abs() > 2]
    print(f'E. ONE SIGNAL AT A TIME (SPX direction, move in bps): {len(fe)} cells; {len(same)} pointed the same way in '
          f'both halves; {len(strong)} of those with |t| > 2 over days (chance alone gives ~{0.05 * len(fe):.0f} at |t| > 2)')
    if len(strong):
        show('   those', strong.sort_values('t', key=abs, ascending=False).head(15).to_dict('records'))

    try:
        import lightgbm  # noqa: F401
    except ImportError:
        print('\nF. skipped: needs lightgbm')
        return
    s5 = allr[allr.hm % 5 == 0].copy()
    rows = []
    for h in (15, 30, 60):
        P = walk_forward(s5, f'spx_{h}', min_train=30, refit=10, features=FEATURES)
        if P is None:
            continue
        P = P[P[f'spx_{h}'].notna()].copy()
        P['model'] = np.where(P.p > 0.5, P[f'spx_{h}'], -P[f'spx_{h}'])
        P['model_conf'] = np.where(P.p >= P.hi_q, P[f'spx_{h}'], np.where(P.p <= P.lo_q, -P[f'spx_{h}'], np.nan))
        P['momentum'] = np.where(P.r15 > 0, P[f'spx_{h}'], -P[f'spx_{h}'])
        for name, col in (('model, every 5 min', 'model'), ('model, its top/bottom 20%', 'model_conf'),
                          ('momentum (last 15 min)', 'momentum')):
            x = P[['day', col]].dropna()
            st = day_stats(P, col)
            rows.append({'hold': h, 'rule': name, 'direction right %': 100 * (x[col] > 0).mean(),
                         'avg bps': x[col].mean(), 'days+': st['days+'], 't': st['t'], 'n': len(x)})
        o = P[P[f'call_{h}'].notna()]
        if len(o):                                        # its picks priced on the days with 0DTE prices
            pick = np.where(o.p > 0.5, o[f'call_{h}'], o[f'put_{h}'])
            rows.append({'hold': h, 'rule': f'  its 0DTE picks, P&L % ({o.day.nunique()} days)',
                         'direction right %': np.nan, 'avg bps': np.nanmean(pick), 'days+': '', 't': np.nan, 'n': len(o)})
    show(f'F. ALL SIGNALS TOGETHER (LightGBM; each day predicted by a model trained only on earlier days, '
         f'{s5.day.nunique() - 30} days out of sample)', rows)
    tests = [r for r in rows if r['t'] == r['t']]               # (the option rows have no t)
    passed = [f'{r["rule"]} {r["hold"]}m' for r in tests if abs(r['t']) > 2]
    print(f'\nRESULT: {len(tests)} tests; about {0.05 * len(tests):.1f} by chance; passed: ' + (' | '.join(passed) or 'none'))


def bid_ask_check():
    """D: unconditional ATM trades at the real ask in / bid out (stream bars, from 10/14)."""
    out = []
    for p in sorted(glob.glob(os.path.join(D, 'option_bars_SPX_2*.csv*'))):
        day = os.path.basename(p)[len('option_bars_SPX_'):][:10]
        o = pd.read_csv(p, usecols=['time', 'expiry', 'strike', 'side', 'mid_close', 'bid', 'ask', 'spot'])
        o = o[(o.expiry == day) & (o.bid > 0) & (o.ask > o.bid)]
        if o.empty:
            continue
        t0 = o.time.min()
        hm = 570 + (o.time - t0) // 60
        o = o.assign(hm=hm)
        piv_b = o.pivot_table(index='time', columns=['side', 'strike'], values='bid', aggfunc='last')
        piv_a = o.pivot_table(index='time', columns=['side', 'strike'], values='ask', aggfunc='last')
        piv_m = o.pivot_table(index='time', columns=['side', 'strike'], values='mid_close', aggfunc='last')
        spot = o.groupby('time').spot.last()
        for t in spot.index:
            m = 570 + (t - t0) // 60
            if not (OPEN <= m <= LAST_ENTRY):
                continue
            k = 5 * round(spot[t] / 5)
            for side in ('call', 'put'):
                if (side, k) not in piv_a or t + 1800 not in piv_b.index:
                    continue
                ask, bid30 = piv_a.at[t, (side, k)], piv_b.at[t + 1800, (side, k)]
                m0, m30 = piv_m.at[t, (side, k)], piv_m.at[t + 1800, (side, k)]
                if ask > 0 and bid30 > 0 and m0 > 0 and m30 > 0:
                    out.append({'day': day, 'side': side, 'real': 100 * (bid30 - ask) / ask, 'model': pct(m0, m30)})
    r = pd.DataFrame(out)
    if r.empty:
        return
    rows = []
    for side, g in r.groupby('side'):
        rows.append({'buy': side, 'hold': 30, 'real ask->bid %': g.real.mean(), 'spread model %': g.model.mean(),
                     'days': g.day.nunique(), 'n': len(g)})
    show('D. CHECK: the same 30-minute trades at the real ask in / bid out (stream bid/ask days)', rows)


if __name__ == '__main__':
    pd.set_option('display.width', 200)
    main()
    bid_ask_check()
