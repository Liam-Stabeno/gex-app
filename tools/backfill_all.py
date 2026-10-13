"""
Get back as much as Schwab still has (run any time; safe to re-run, everything dedups):

  1. 1-minute trade bars for every listed SPX option contract within --band points,
     all expirations, as far back as Schwab keeps them (~31 trading days)
  2. on a trading day before the close: today's 0DTE again at 15:55 (contracts vanish
     at expiry) and once more at 16:01 if Schwab still lists them
  3. price history for SPX, ES, the VIX family, TICK, TRIN, SPY, QQQ, NDX, RUT at every
     interval Schwab offers (market_history.backfill)

    python tools/backfill_all.py [--band 300] [--rate 1.2] [--skip-options]
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
os.environ.setdefault('GEX_NO_AUTO_REAUTH', '1')

import market_history as mh                           # noqa: E402
import option_history as oh                           # noqa: E402
from gex import get_access_token, fetch_option_chain  # noqa: E402

ET = oh.ET


def at(h, m, s=0):
    n = datetime.now(ET)
    return n.replace(hour=h, minute=m, second=s, microsecond=0).timestamp()


def sleep_until(ts):
    while time.time() < ts:
        time.sleep(min(30, ts - time.time()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--band', type=float, default=300)
    ap.add_argument('--rate', type=float, default=1.2)
    ap.add_argument('--days', type=int, default=45)
    ap.add_argument('--skip-options', action='store_true')
    a = ap.parse_args()
    t0 = time.time()

    with oh.history_lock(wait=7200) as lock:
        if not lock.ok:
            sys.exit('another option-history run holds the lock')
        if not a.skip_options:
            chain = fetch_option_chain('$SPX', get_access_token(), strike_count=150)
            spot, now = chain['underlyingPrice'], datetime.now(ET)
            today = now.date().isoformat()
            zero = oh.contracts_from_chain(chain, spot, spot * 0.05, expiries={today})
            rest = [c for c in oh.contracts_from_chain(chain, spot, a.band) if c[1] != today]
            start_ms = int((now - timedelta(days=a.days)).timestamp() * 1000)
            before_close = bool(zero) and time.time() < at(15, 55)
            print(f'spot {spot:.2f}: {len(rest)} contracts in later expirations, {len(zero)} 0DTE', flush=True)

            s = oh.pull(rest, start_ms, int(time.time() * 1000), rate=a.rate,
                        stop_at=at(15, 54, 30) if before_close else None)
            done = s['contracts']
            print(f'[backfill] later expirations: {s}', flush=True)

            if before_close:
                sleep_until(at(15, 55))
                day0, _ = oh.day_range_ms(now.date())
                s0 = oh.pull(zero, day0, int(time.time() * 1000), rate=1.6, stop_at=at(15, 59, 50))
                print(f'[backfill] 0DTE before expiry: {s0}', flush=True)
                sleep_until(at(16, 1))
                probe = oh.fetch_bars(zero[0][0], int(at(15, 55) * 1000), int(time.time() * 1000))
                if probe:
                    s1 = oh.pull(zero, int(at(15, 50) * 1000), int(time.time() * 1000), rate=1.6)
                    print(f'[backfill] 0DTE after the close (still listed): {s1}', flush=True)
                else:
                    print('[backfill] 0DTE no longer served after the close', flush=True)
                if done < len(rest):
                    s2 = oh.pull(rest[done:], start_ms, int(time.time() * 1000), rate=a.rate)
                    print(f'[backfill] later expirations, rest: {s2}', flush=True)

    print('[backfill] price history (SPX, ES, VIX family, internals, ETFs):', flush=True)
    mh.backfill()
    print(f'[backfill] all done in {(time.time() - t0) / 60:.0f} min', flush=True)


if __name__ == '__main__':
    main()
