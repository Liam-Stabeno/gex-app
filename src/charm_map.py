"""
Charm map: the dealers' hedge flow from time decay alone at each price near spot, and the
levels where it turns.

  PULL   flow BUY just below, SELL just above: decay pushes price up toward it from below
         and down toward it from above, so late in the day it's where the drift heads
  SPLIT  the reverse (SELL below, BUY above): decay pushes price away from it

Dealers are taken as long calls / short puts (bs.dealer_charm_flow). Each contract keeps
its own vol at every price (sticky strike). The flow is $ of SPX per hour of clock time.

contracts: [(strike, side, oi, sigma, T years)] — the live loop builds them from its solved
vols, watchlist_contracts() from a saved gex_watchlist snapshot (vol backed out of delta).
"""
import math
from datetime import date, datetime, time as dtime
from statistics import NormalDist
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo('America/New_York')
R = 0.05                 # as the live loop (background._RISK_FREE)
SPAN_PTS = 40            # the map covers spot ± this
STEP_PTS = 1.0
HOURS_PER_YEAR = 365.0 * 24
_N = NormalDist()


def _arrays(contracts):
    k, side, oi, v, t = zip(*contracts)
    sign = np.where(np.array(side) == 'call', 1.0, -1.0)
    return np.array(k, float), sign, np.array(oi, float), np.array(v, float), np.array(t, float)


def flow_at(contracts, S: float, r: float = R) -> float:
    """dealers' charm hedge flow at price S, $ per hour (+ = they buy). Vectorised
    bs.dealer_charm_flow(bs._bs_charm(...)) over the contracts."""
    if not contracts:
        return 0.0
    k, sign, oi, v, t = _arrays(contracts)
    return float(_flows(k, sign, oi, v, t, np.array([S]), r)[0])


def _flows(k, sign, oi, v, t, spots, r):
    ok = (t > 0) & (v > 0) & (oi > 0)
    k, sign, oi, v, t = k[ok], sign[ok], oi[ok], v[ok], t[ok]
    if not len(k):
        return np.zeros(len(spots))
    S = spots[:, None]
    sq = np.sqrt(t)
    d1 = (np.log(S / k) + (r + 0.5 * v * v) * t) / (v * sq)
    d2 = d1 - v * sq
    pdf = np.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    charm = -pdf * (2.0 * r * t - d2 * v * sq) / (2.0 * t * v * sq)       # bs._bs_charm, per year
    flow = -charm * oi * 100.0 * S * sign                                  # bs.dealer_charm_flow
    return flow.sum(axis=1) / HOURS_PER_YEAR


def charm_map(contracts, spot: float, span: float = SPAN_PTS, step: float = STEP_PTS, r: float = R) -> dict:
    """{'spots': [...], 'flow': [$ per hour at each], 'pull': [levels], 'split': [levels]},
    the levels interpolated between grid steps, nearest spot first"""
    n = int(round(span / step))
    spots = np.array([spot + i * step for i in range(-n, n + 1)])
    flow = _flows(*_arrays(contracts), spots, r) if contracts else np.zeros(len(spots))
    pull, split = [], []
    for i in range(len(spots) - 1):
        f0, f1 = flow[i], flow[i + 1]
        if (f0 > 0) == (f1 > 0) or f0 == f1:
            continue
        x = float(spots[i] + (spots[i + 1] - spots[i]) * f0 / (f0 - f1))
        (pull if f0 > 0 else split).append(round(x, 1))
    near = lambda xs: sorted(xs, key=lambda x: abs(x - spot))
    return {'spots': [round(float(s), 2) for s in spots], 'flow': [round(float(f)) for f in flow],
            'pull': near(pull), 'split': near(split)}


# ── From a saved gex_watchlist snapshot (delta + open interest per contract) ─────────────
def sigma_from_delta(delta: float, side: str, S: float, K: float, T: float, r: float = R):
    """Vol from an OTM delta: d1 = N^-1(delta), then the unique positive root."""
    p = delta if side == 'call' else delta + 1.0
    if not (0.01 < p < 0.99):
        return None
    d1 = _N.inv_cdf(p)
    a, b, c = T / 2.0, -d1 * math.sqrt(T), math.log(S / K) + r * T
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    pos = [x for x in ((-b + s * math.sqrt(disc)) / (2 * a) for s in (1, -1)) if x > 0]
    if not pos:
        return None
    v = min(pos) if c > 0 else max(pos)
    return v if 0.01 < v < 3.0 else None


PT = ZoneInfo('America/Los_Angeles')   # gex_watchlist timestamps are the PC's local (Pacific) time
SNAP_FRESH_SEC = 600


def watchlist_snapshot(day: str, t: float, data_dir: str):
    """(rows, its time) of the gex_watchlist snapshot last at or before epoch t, within
    SNAP_FRESH_SEC, else None. Only lines in that window are parsed (a day is ~100k rows)."""
    import csv
    from gex_stats import day_file, open_text
    path = day_file(f'gex_watchlist_SPX_{day}.csv', data_dir)
    if not path:
        return None
    hi = datetime.fromtimestamp(t, PT).strftime('%Y-%m-%d %H:%M:%S')
    lo = datetime.fromtimestamp(t - SNAP_FRESH_SEC, PT).strftime('%Y-%m-%d %H:%M:%S')
    best, rows = None, []
    with open_text(path) as f:
        reader = csv.reader(f)
        cols = {n: i for i, n in enumerate(next(reader, []))}
        need = ('timestamp', 'symbol', 'strike', 'side', 'delta', 'oi')
        if not all(n in cols for n in need):
            return None
        for parts in reader:
            stamp = parts[cols['timestamp']] if len(parts) > cols['timestamp'] else ''
            if not (lo <= stamp <= hi):
                continue
            if stamp != best:
                if best is not None and stamp < best:
                    continue
                best, rows = stamp, []
            try:
                rows.append({n: parts[cols[n]] for n in need})
            except IndexError:
                continue
    if best is None:
        return None
    ts = datetime.strptime(best, '%Y-%m-%d %H:%M:%S').replace(tzinfo=PT).astimezone(ET)
    return rows, ts


def _expiry(symbol: str) -> date:
    return datetime.strptime(symbol.split()[-1][:6], '%y%m%d').date()


def watchlist_contracts(rows, spot: float, ts: datetime, zero_dte_only: bool = True) -> list:
    """contracts from one watchlist snapshot's rows (dicts with symbol, strike, side, delta,
    oi): one vol per strike and expiry, backed out of its OTM side's delta (the ITM side as
    a fallback), as the live loop solves it from the OTM quote"""
    cells = {}
    for r in rows:
        try:
            exp = _expiry(r['symbol'])
        except (ValueError, IndexError, AttributeError):
            continue
        if zero_dte_only and exp != ts.astimezone(ET).date():
            continue
        cells.setdefault((exp, float(r['strike'])), {})[r['side']] = (float(r['delta']), float(r['oi'] or 0))
    out = []
    for (exp, k), sides in cells.items():
        secs = (datetime.combine(exp, dtime(16, 0), ET) - ts).total_seconds()
        if secs <= 0:
            continue
        T = max(secs, 300.0) / (HOURS_PER_YEAR * 3600)
        otm = 'call' if k >= spot else 'put'
        sigma = None
        for side in (otm, 'put' if otm == 'call' else 'call'):
            if side in sides and (sigma := sigma_from_delta(sides[side][0], side, spot, k, T)):
                break
        if not sigma:
            continue
        out += [(k, side, oi, sigma, T) for side, (_, oi) in sides.items() if oi > 0]
    return out
