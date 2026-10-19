"""
Rebuild the gamma heatmap's snapshots for minutes the app wasn't running, e.g.
2026-10-19 09:30-12:56 (the PC was asleep at the 6:14 scheduled start).

Open interest is fixed for the day, so each missing minute's GEX by strike is worked
out from:
  - the day's first full-chain snapshot (data/gex_full_SPX_<date>.jsonl): open interest
    and implied vol for every contract;
  - SPX 1-minute closes (price history) for spot;
  - the day's 1-minute option trade bars once the 17:15 history job has saved them
    (data/option_trade_bars_SPX_<date>.csv): each contract's vol is solved from its
    latest trade, so vol moves through the morning. Without them the snapshot's vol
    is held (spot and time still move).
Gamma is Black-Scholes rounded to 3 decimals, as Schwab's chain gamma is (the live grid
uses those), so rebuilt columns join the recorded ones without a jump. Only the
contracts (expiry, strike) in the day's first recorded snapshot are used.

Output: data/gex_grid_rebuilt_SPX_<date>.jsonl, the grid's line format plus a "rebuilt"
field. Only minutes before the first recorded snapshot are written; recorded files are
never touched. Re-running replaces the rebuilt file (derived data, not market data).

    python tools/rebuild_gex_grid.py 2026-10-19            # rebuild
    python tools/rebuild_gex_grid.py 2026-10-19 --check    # compare with a recorded snapshot
"""
import argparse
import csv
import json
import os
import sys
from bisect import bisect_right
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
import bs                                   # noqa: E402
from gex_stats import day_file, open_text   # noqa: E402
from price_history import load_range        # noqa: E402

ET = ZoneInfo('America/New_York')
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
RISK_FREE = 0.05                            # background._RISK_FREE
SYM = 'SPX'


def first_line(path: str) -> dict:
    with open_text(path) as f:
        for line in f:
            if line.strip():
                return json.loads(line)
    raise ValueError(f'{path} is empty')


def load_base(day: str, data_dir: str):
    """(first recorded grid snapshot, contracts from the first full-chain snapshot).
    contracts: [(expiry, strike, 'c'|'p', oi, iv decimal)] for the grid's (expiry, strike)."""
    grid_path = day_file(f'gex_grid_{SYM}_{day}.jsonl', data_dir)
    full_path = day_file(f'gex_full_{SYM}_{day}.jsonl', data_dir)
    if not grid_path or not full_path:
        raise SystemExit(f'{day}: needs gex_grid_{SYM} and gex_full_{SYM} files')
    grid = first_line(grid_path)
    keys = {(grid['exp'][r[1]], float(r[0])) for r in grid['rows']}
    full = first_line(full_path)
    f = {name: i for i, name in enumerate(full['fields'])}
    contracts = []
    for r in full['rows']:
        exp, k = full['exp'][r[f['exp_idx']]], float(r[f['strike']])
        oi = int(r[f['oi']] or 0)
        if (exp, k) not in keys or oi <= 0:
            continue
        iv = r[f['iv']]
        iv = float(iv) / 100.0 if iv not in (None, '') and 0 < float(iv) < 500 else None
        contracts.append((exp, k, 'c' if r[f['side']] == 0 else 'p', oi, iv))
    return grid, full, contracts


def load_bars(day: str, data_dir: str) -> dict:
    """(expiry, strike, 'c'|'p') -> sorted [(epoch s, close)] from the day's 1-minute trade bars."""
    path = day_file(f'option_trade_bars_{SYM}_{day}.csv', data_dir)
    out = {}
    if not path:
        return out
    with open_text(path) as fh:
        for r in csv.DictReader(fh):
            try:
                key = (r['expiry'], float(r['strike']), r['side'][0])
                out.setdefault(key, []).append((int(r['time']), float(r['close'])))
            except (KeyError, ValueError):
                continue
    for v in out.values():
        v.sort()
    return out


def grid_rows(contracts, spot: float, when: datetime, exps: list, iv_at=None) -> tuple:
    """Grid rows [[strike, exp_idx, call_oi, put_oi, gex]] at `when` and `spot`.
    iv_at(contract) -> vol to use, or None for the snapshot's. Returns (rows, n_iv_from_bars)."""
    idx = {e: i for i, e in enumerate(exps)}
    cells, n_bars = {}, 0
    T_by_exp = {}
    for c in contracts:
        exp, k, side, oi, iv = c
        live = iv_at(c) if iv_at else None
        if live is not None:
            iv, n_bars = live, n_bars + 1
        if exp not in T_by_exp:
            T_by_exp[exp] = bs.t_to_close(exp, now=when)
        T = T_by_exp[exp]
        g = round(bs._bs_gamma(spot, k, T, RISK_FREE, iv), 3) if iv and T > 0 else 0.0
        cell = cells.setdefault((exp, k), [0, 0, 0.0])
        cell[0 if side == 'c' else 1] += oi
        cell[2] += (g if side == 'c' else -g) * oi * 100 * spot
    rows = [[k, idx[exp], co, po, round(gex)] for (exp, k), (co, po, gex) in cells.items()]
    return rows, n_bars


def bar_iv_lookup(bars: dict, spot_at):
    """iv_at(contract, ts) from the latest trade at or before ts (solved once per bar)."""
    solved = {}

    def iv_at(c, ts):
        exp, k, side = c[0], c[1], c[2]
        seq = bars.get((exp, k, side))
        if not seq:
            return None
        i = bisect_right(seq, (ts, float('inf'))) - 1
        if i < 0:
            return None
        t, px = seq[i]
        key = (exp, k, side, t)
        if key not in solved:
            S = spot_at(t)
            T = bs.t_to_close(exp, now=datetime.fromtimestamp(t + 59, ET))
            solved[key] = bs.iv(px, S, k, T, RISK_FREE, side) if S else None
        return solved[key]
    return iv_at


def check(day: str, data_dir: str):
    """Rebuild the recorded grid snapshot nearest the full-chain snapshot and compare."""
    grid, full, contracts = load_base(day, data_dir)
    when = datetime.fromisoformat(full['ts'])
    best = None
    with open_text(day_file(f'gex_grid_{SYM}_{day}.jsonl', data_dir)) as f:
        for line in f:
            s = json.loads(line)
            d = abs((datetime.fromisoformat(s['ts']) - when).total_seconds())
            if best is None or d < best[0]:
                best = (d, s)
            elif d > best[0]:
                break
    real = best[1]
    rows, _ = grid_rows(contracts, float(real['spot']), datetime.fromisoformat(real['ts']), real['exp'])
    got = {(real['exp'][r[1]], r[0]): r[4] for r in rows}
    rec = {(real['exp'][r[1]], float(r[0])): r[4] for r in real['rows']}
    keys = sorted(set(got) | set(rec))
    a = [rec.get(k, 0) for k in keys]
    b = [got.get(k, 0) for k in keys]

    def per_strike(src, odte):
        out = {}
        for (exp, k), v in src.items():
            if (exp == day) == odte:
                out[k] = out.get(k, 0) + v
        return out

    print(f'recorded {real["ts"]} spot {real["spot"]} vs rebuilt from full chain {full["ts"]}')
    print(f'contracts {len(contracts)}, cells recorded {len(rec)} rebuilt {len(got)}')
    print(f'total |GEX| recorded {sum(map(abs, a)) / 1e6:,.0f}M  rebuilt {sum(map(abs, b)) / 1e6:,.0f}M')
    print(f'net GEX     recorded {sum(a) / 1e6:,.0f}M  rebuilt {sum(b) / 1e6:,.0f}M')
    for odte in (True, False):
        r, g = per_strike(rec, odte), per_strike(got, odte)
        top = sorted(r, key=lambda k: -abs(r[k]))[:6]
        print(('0DTE' if odte else 'other expirations') + ' - biggest recorded strikes, recorded vs rebuilt (M):')
        print('   ' + '  '.join(f'{k:g}: {r[k] / 1e6:,.0f} / {g.get(k, 0) / 1e6:,.0f}' for k in top))


def rebuild(day: str, data_dir: str) -> str:
    grid, full, contracts = load_base(day, data_dir)
    first_ts = int(datetime.fromisoformat(grid['ts']).timestamp())
    d = date.fromisoformat(day)
    open_ms = int(datetime.combine(d, dtime(9, 30), ET).timestamp() * 1000)
    candles = [c for c in load_range('$SPX', open_ms, first_ts * 1000)
               if c['datetime'] // 1000 + 59 < first_ts]
    if not candles:
        raise SystemExit(f'{day}: nothing to rebuild (recorded from {grid["ts"]})')
    closes = {c['datetime'] // 1000: float(c['close']) for c in candles}
    spot_at = lambda t: closes.get(t - t % 60)
    bars = load_bars(day, data_dir)
    lookup = bar_iv_lookup(bars, spot_at) if bars else None

    out_path = os.path.join(data_dir, f'gex_grid_rebuilt_{SYM}_{day}.jsonl')
    tmp = out_path + '.tmp'
    n_lines = 0
    with open(tmp, 'w', encoding='utf-8') as f:
        for c in candles:
            ts = c['datetime'] // 1000 + 59              # the minute's close, like a chain fetch
            when = datetime.fromtimestamp(ts, ET)
            spot = float(c['close'])
            iv_at = (lambda con, ts=ts: lookup(con, ts)) if lookup else None
            rows, n_bars = grid_rows(contracts, spot, when, grid['exp'], iv_at)
            f.write(json.dumps({'ts': when.isoformat(timespec='seconds'), 'spot': spot, 'exp': grid['exp'],
                                'rows': rows, 'rebuilt': {'iv_from_trades': n_bars, 'contracts': len(contracts),
                                                          'base': full['ts']}},
                               separators=(',', ':')) + '\n')
            n_lines += 1
    os.replace(tmp, out_path)
    src = f'vol from {len(bars):,} contracts\' trades' if bars else 'vol held from the snapshot (no trade bars yet)'
    print(f'{day}: rebuilt {n_lines} minutes {datetime.fromtimestamp(candles[0]["datetime"] // 1000, ET):%H:%M}'
          f'-{datetime.fromtimestamp(candles[-1]["datetime"] // 1000, ET):%H:%M} from {len(contracts):,} contracts, '
          f'{src} -> {os.path.basename(out_path)}')
    return out_path


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('day')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--data-dir', default=DATA_DIR)
    a = ap.parse_args()
    (check if a.check else rebuild)(a.day, os.path.abspath(a.data_dir))
