"""
Repair saved 1-min candles that were frozen mid-minute.

Until 2026-10-07 the price sync saved Schwab's in-progress minute, and because saving
skips minutes that already exist, the final OHLC/volume never replaced it (about 2/3 of
minutes on 2026-10-06 differed; ES volume under-counted on 319). This re-downloads every
day Schwab still serves and overwrites those minutes with the complete bars.

    python tools/repair_price_history.py            # $SPX and /ES, as far back as Schwab serves
    python tools/repair_price_history.py --days 10

Stop the dashboard first (it writes to the same files). Originals are copied to
data/_backup_price_history_<timestamp>/ (gzipped) before anything changes.
"""
import argparse
import gzip
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
from gex import get_access_token                                     # noqa: E402
from price_history import csv_path, fetch_candles_range, replace_candles  # noqa: E402

ET = ZoneInfo('America/New_York')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=60, help='calendar days back to try')
    ap.add_argument('--symbols', nargs='+', default=['$SPX', '/ES'])
    a = ap.parse_args()

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    bdir = os.path.join(ROOT, 'data', f'_backup_price_history_{stamp}')
    os.makedirs(bdir, exist_ok=True)
    for sym in a.symbols:
        src = csv_path(sym)
        if os.path.exists(src):
            with open(src, 'rb') as f, gzip.open(os.path.join(bdir, os.path.basename(src) + '.gz'), 'wb') as g:
                shutil.copyfileobj(f, g)
    print(f'backup: {bdir}')

    token = get_access_token()
    today = datetime.now(ET).date()
    for sym in a.symbols:
        total, served_days, oldest, empty_run = 0, 0, None, 0
        for back in range(a.days + 1):
            day = today - timedelta(days=back)
            if day.weekday() == 5:                       # Saturday: no session for either symbol
                continue
            start = int(datetime.combine(day, datetime.min.time(), ET).timestamp() * 1000)
            candles = fetch_candles_range(sym, token, start, start + 86_400_000 - 1)
            time.sleep(0.6)                              # stay well clear of rate limits
            if not candles:
                empty_run += 1
                if empty_run >= 5:                       # past what Schwab serves
                    break
                continue
            empty_run = 0
            changed = replace_candles(sym, candles)
            total += changed
            served_days += 1
            oldest = day
            print(f'  {sym} {day}: {len(candles):4d} bars from Schwab, {changed:4d} saved minutes corrected/added')
        print(f'{sym}: {served_days} days repaired back to {oldest}, {total} minutes corrected/added\n')


if __name__ == '__main__':
    main()
