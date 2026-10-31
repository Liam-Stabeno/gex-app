"""
Build data/charm_map_SPX_<date>.jsonl.gz for days from before the app saved its charm maps,
from each gex_watchlist snapshot of the session (0DTE delta + open interest, the vol backed
out of the delta). Lines carry src='watchlist'. A day that already has a file is left alone.

flow_all is the snapshot's charm_flow, with the sign fixed for days before
gex_stats.CHARM_FIXED_FROM.

Run:  python tools/rebuild_charm_map.py      (the .venv or the system python)
"""
import csv
import glob
import gzip
import json
import multiprocessing
import os
import sys
from datetime import date, datetime, time as dtime

import pandas as pd

ROOT = r'C:\Productivity\TradingApp'
sys.path.insert(0, ROOT + r'\src')
import charm_map as cm  # noqa: E402
from gex_stats import CHARM_FIXED_FROM, _DATA_DIR, day_file, open_text  # noqa: E402

FRESH_SEC = 600            # a gex snapshot further than this from the watchlist's: no spot


def rebuild(day: str) -> tuple:
    out = os.path.join(_DATA_DIR, f'charm_map_SPX_{day}.jsonl')
    if day_file(os.path.basename(out)):
        return day, 'kept'
    snap = day_file(f'gex_snapshots_SPX_{day}.csv')
    watch = day_file(f'gex_watchlist_SPX_{day}.csv')
    if not snap or not watch:
        return day, 'no data'
    s = pd.read_csv(snap, usecols=lambda c: c in ('timestamp', 'spot', 'charm_flow'))
    s['t'] = pd.to_datetime(s['timestamp'])
    s = s.sort_values('t').reset_index(drop=True)
    sign = -1 if date.fromisoformat(day) < CHARM_FIXED_FROM else 1
    stamps = {}
    with open_text(watch) as f:
        for r in csv.DictReader(f):
            stamps.setdefault(r['timestamp'], []).append(r)
    lines = []
    for stamp, rows in sorted(stamps.items()):
        ts = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').replace(tzinfo=cm.PT).astimezone(cm.ET)
        if not dtime(9, 30) <= ts.time() < dtime(16, 0):
            continue
        i = s['t'].searchsorted(pd.Timestamp(stamp), side='right') - 1
        if i < 0 or (pd.Timestamp(stamp) - s['t'].iloc[i]).total_seconds() > FRESH_SEC:
            continue
        spot = float(s['spot'].iloc[i])
        contracts = cm.watchlist_contracts(rows, spot, ts)
        if not contracts:
            continue
        cf = s['charm_flow'].iloc[i] if 'charm_flow' in s else None
        line = cm.record_line(ts, spot, cm.charm_map(contracts, spot), contracts,
                              None if cf is None or pd.isna(cf) else sign * float(cf), src='watchlist')
        lines.append(json.dumps(line, separators=(',', ':')))
    if not lines:
        return day, 'no 0DTE snapshots'
    with gzip.open(out + '.gz', 'wt', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return day, f'{len(lines)} lines'


def main():
    days = sorted(os.path.basename(f)[len('gex_watchlist_SPX_'):][:10]
                  for f in glob.glob(os.path.join(_DATA_DIR, 'gex_watchlist_SPX_2*')))
    with multiprocessing.Pool(max(2, (os.cpu_count() or 4) // 2)) as pool:
        for day, what in pool.imap(rebuild, days):
            print(day, what, flush=True)


if __name__ == '__main__':
    main()
