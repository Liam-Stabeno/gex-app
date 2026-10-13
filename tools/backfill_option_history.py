"""
Pull Schwab's 1-minute trade bars for every listed SPX option contract within --band
points of the price, into data/option_trade_bars_SPX_<date>.csv (merged, deduped).

    python tools/backfill_option_history.py --today      # today's 0DTE only (before 16:00!)
    python tools/backfill_option_history.py              # all listed expirations, as far back as Schwab has
    python tools/backfill_option_history.py --band 200 --rate 1.5

Expired contracts can't be fetched (Schwab drops them), so past 0DTE days are gone;
for listed contracts Schwab keeps about 31 trading days of 1-minute bars.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
os.environ.setdefault('GEX_NO_AUTO_REAUTH', '1')     # a script never pops the login window

import option_history as oh                           # noqa: E402
from gex import get_access_token, fetch_option_chain  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--today', action='store_true', help="only today's 0DTE (today's bars)")
    ap.add_argument('--band', type=float, default=300, help='strikes within this of spot')
    ap.add_argument('--rate', type=float, default=1.0, help='requests per second')
    ap.add_argument('--days', type=int, default=45, help='calendar days back (Schwab keeps ~31 trading days)')
    a = ap.parse_args()

    chain = fetch_option_chain('$SPX', get_access_token(), strike_count=150)
    spot = chain['underlyingPrice']
    now = datetime.now(oh.ET)
    today = now.date().isoformat()
    if a.today:
        contracts = oh.contracts_from_chain(chain, spot, spot * 0.05, expiries={today})
        start_ms, _ = oh.day_range_ms(now.date())
    else:
        contracts = oh.contracts_from_chain(chain, spot, a.band)
        start_ms = int((now - timedelta(days=a.days)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    print(f'spot {spot:.2f}: {len(contracts)} contracts, '
          f'~{len(contracts) / a.rate / 60:.0f} min at {a.rate}/s', flush=True)
    t0 = time.time()
    s = oh.pull(contracts, start_ms, end_ms, rate=a.rate)
    print(f"done in {(time.time() - t0) / 60:.1f} min: {s['contracts']} contracts, "
          f"{s['with_bars']} traded, {s['bars']} bars, {s['added']} new rows saved")


if __name__ == '__main__':
    main()
