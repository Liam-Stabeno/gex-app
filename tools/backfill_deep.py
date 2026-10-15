"""
Get back as much option and market history as Schwab still has, beyond
tools/backfill_all.py. Resumable (data/backfill_progress.json) and safe to re-run
(everything dedups); stops at --until so the app's daily history jobs get the lock.

  1. price history for every symbol in market_history.SYMBOLS (1-min, 5-min, daily)
  2. 1-minute bars for every contract expiring within 30 days, within --band-pct of
     spot, that tools/backfill_all.py's +-300 pts didn't cover
  3. 5-minute bars (Schwab: back to ~January) for contracts expiring beyond 30 days,
     plus the monthly / quarterly ones inside 30 days (listed long before)
  4. daily bars (~2 years) for the same contracts

    python tools/backfill_deep.py [--band-pct 10] [--rate 1.5] [--until 09:00]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
os.environ.setdefault('GEX_NO_AUTO_REAUTH', '1')

import market_history as mh                         # noqa: E402
import option_history as oh                         # noqa: E402
from gex import SCHWAB_TIMEOUT, get_access_token    # noqa: E402

ET = oh.ET
PROGRESS = os.path.join(oh.DATA_DIR, 'backfill_progress.json')


def load_progress() -> dict:
    try:
        with open(PROGRESS) as f:
            return {k: set(v) for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return {}


def save_progress(p: dict):
    tmp = PROGRESS + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({k: sorted(v) for k, v in p.items()}, f)
    os.replace(tmp, PROGRESS)


def until_ts(hhmm: str) -> float:
    h, m = map(int, hhmm.split(':'))
    now = datetime.now(ET)
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.timestamp()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--band-pct', type=float, default=10.0)
    ap.add_argument('--rate', type=float, default=1.5)
    ap.add_argument('--until', default='09:00', help='ET time to stop by (next occurrence)')
    ap.add_argument('--skip-market', action='store_true')
    a = ap.parse_args()
    stop_at, t0 = until_ts(a.until), time.time()
    print(f"[deep] stopping by {datetime.fromtimestamp(stop_at, ET):%a %H:%M} ET", flush=True)

    if not a.skip_market:
        print('[deep] 1. price history for every symbol', flush=True)
        mh.backfill()

    with oh.history_lock(wait=1800) as lock:
        if not lock.ok:
            sys.exit('another option-history run holds the lock')
        contracts, spot = oh.all_listed_contracts(a.band_pct)
        today = datetime.now(ET).date()
        near_cut = (today + timedelta(days=30)).isoformat()
        near = [c for c in contracts if c[1] <= near_cut]
        deep = [c for c in contracts if c[1] > near_cut or c[4] in ('S', 'Q')]   # + monthly / quarterly
        prog = load_progress()
        print(f'[deep] spot {spot:.2f}: {len(contracts):,} contracts within {a.band_pct:g}%, '
              f'{len(near):,} within 30 days, {len(deep):,} for 5-min / daily', flush=True)

        now_ms = int(time.time() * 1000)
        stages = [
            ('1m', [c for c in near if abs(c[2] - spot) > 300],
             int((datetime.now(ET) - timedelta(days=45)).timestamp() * 1000)),
            ('5m', deep, int(datetime(today.year - 1, today.month, 1, tzinfo=ET).timestamp() * 1000)),
            ('1d', deep, int(datetime(today.year - 3, 1, 1, tzinfo=ET).timestamp() * 1000)),
        ]
        for n, (size, todo, start_ms) in enumerate(stages, 2):
            done = prog.setdefault(size, set())
            todo = [c[:4] for c in todo if c[0] not in done]
            print(f'[deep] {n}. {size} bars: {len(todo):,} contracts to go '
                  f'(~{len(todo) / a.rate / 60:.0f} min)', flush=True)

            def record(symbols, size=size):
                prog[size].update(symbols)
                save_progress(prog)

            s = oh.pull(todo, start_ms, now_ms, rate=a.rate, stop_at=stop_at, size=size, on_saved=record)
            print(f'[deep] {size}: {s}', flush=True)
            if time.time() >= stop_at:
                print('[deep] deadline reached: re-run to continue where it stopped', flush=True)
                break
    print(f'[deep] finished in {(time.time() - t0) / 60:.0f} min', flush=True)


if __name__ == '__main__':
    main()
