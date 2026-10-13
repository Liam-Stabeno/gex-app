"""
option_history.py — Schwab's 1-minute trade bars for SPX option contracts, saved per
day to data/option_trade_bars_SPX_<date>.csv (time, symbol, expiry, strike, side,
open, high, low, close, volume; only minutes that traded).

Schwab serves this history only while a contract is listed (about 31 trading days of
1-minute bars); once it expires, it's gone. So today's 0DTE is pulled shortly before
the close, the other expirations after it (background.option_history_loop), and
tools/backfill_option_history.py pulls whatever is still listed.

Requests are paced (default 1 per second) so the live app keeps headroom under
Schwab's ~120 requests/minute; a 429 pauses a minute and retries.
"""
import csv
import os
import time
from datetime import date, datetime

import requests

from gex import SCHWAB_TIMEOUT, get_access_token

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(_SRC_DIR), 'data')
FIELDS = ['time', 'symbol', 'expiry', 'strike', 'side', 'open', 'high', 'low', 'close', 'volume']

from zoneinfo import ZoneInfo
ET = ZoneInfo('America/New_York')


def contracts_from_chain(chain: dict, spot: float, band: float, expiries=None, traded_only=False) -> list:
    """[(symbol, expiry 'YYYY-MM-DD', strike, side)] within `band` pts of spot, for the
    given expiry dates (None = all), nearest expiry first. traded_only: totalVolume > 0."""
    out = []
    for side, key in (('call', 'callExpDateMap'), ('put', 'putExpDateMap')):
        for exp_key, strikes in (chain.get(key) or {}).items():
            exp = exp_key.split(':')[0]
            if expiries is not None and exp not in expiries:
                continue
            for strike_s, opts in strikes.items():
                k = float(strike_s)
                if abs(k - spot) > band:
                    continue
                for o in opts or []:
                    if o.get('symbol') and (not traded_only or (o.get('totalVolume') or 0) > 0):
                        out.append((o['symbol'], exp, k, side))
    out.sort(key=lambda c: (c[1], abs(c[2] - spot), c[3]))
    return out


def fetch_bars(symbol: str, start_ms: int, end_ms: int, token: str | None = None) -> list:
    """1-minute trade bars for one contract between start_ms and end_ms ([] if none)."""
    for attempt in range(3):
        r = requests.get('https://api.schwabapi.com/marketdata/v1/pricehistory', timeout=SCHWAB_TIMEOUT,
                         headers={'Authorization': f'Bearer {token or get_access_token()}'},
                         params={'symbol': symbol, 'frequencyType': 'minute', 'frequency': 1,
                                 'startDate': int(start_ms), 'endDate': int(end_ms)})
        if r.status_code == 429:
            print(f'[option_history] rate limited: pausing 60 s ({symbol})')
            time.sleep(60)
            continue
        if not r.ok:
            return []
        return r.json().get('candles', []) or []
    return []


def save_bars(rows_by_day: dict, data_dir: str | None = None) -> int:
    """Merge rows into each day's file (deduped on time + symbol; existing rows kept).
    Returns rows added."""
    added = 0
    for day, rows in rows_by_day.items():
        path = os.path.join(data_dir or DATA_DIR, f'option_trade_bars_SPX_{day}.csv')
        have = {}
        if os.path.exists(path):
            with open(path, newline='', encoding='utf-8') as f:
                for r in csv.reader(f):
                    if r and r[0] != 'time':
                        have[(r[0], r[1])] = r
        for r in rows:
            key = (str(r[0]), r[1])
            if key not in have:
                have[key] = [str(x) for x in r]
                added += 1
        tmp = path + '.tmp'
        with open(tmp, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f, lineterminator='\n')
            w.writerow(FIELDS)
            w.writerows(sorted(have.values(), key=lambda r: (int(r[0]), r[1])))
        os.replace(tmp, path)
    return added


def pull(contracts: list, start_ms: int, end_ms: int, rate: float = 1.0, log_every: int = 100,
         data_dir: str | None = None, stop_at: float | None = None) -> dict:
    """Fetch and save bars for each contract. Saves every `log_every` contracts so a
    stopped run keeps what it got. stop_at: epoch s to stop by. Returns counts."""
    token, token_at = get_access_token(), time.time()
    pending, stats = {}, {'contracts': 0, 'with_bars': 0, 'bars': 0, 'added': 0}
    gap = 1.0 / rate if rate > 0 else 0
    for i, (sym, exp, k, side) in enumerate(contracts, 1):
        if stop_at and time.time() >= stop_at:
            print(f'[option_history] stopped at the deadline after {i - 1} contracts')
            break
        if time.time() - token_at > 600:
            token, token_at = get_access_token(), time.time()
        t0 = time.time()
        bars = fetch_bars(sym, start_ms, end_ms, token)
        stats['contracts'] += 1
        if bars:
            stats['with_bars'] += 1
            stats['bars'] += len(bars)
            for b in bars:
                day = datetime.fromtimestamp(b['datetime'] / 1000, ET).date().isoformat()
                pending.setdefault(day, []).append([int(b['datetime'] // 1000), sym, exp, k, side,
                                                    b['open'], b['high'], b['low'], b['close'], b['volume']])
        if i % log_every == 0:
            stats['added'] += save_bars(pending, data_dir)
            pending = {}
            print(f"[option_history] {i}/{len(contracts)} contracts, {stats['bars']} bars", flush=True)
        time.sleep(max(0.0, gap - (time.time() - t0)))
    stats['added'] += save_bars(pending, data_dir)
    return stats


LOCK_PATH = os.path.join(DATA_DIR, 'option_history.lock')
LOCK_STALE_SEC = 3 * 3600


class history_lock:
    """One writer at a time (the app's daily passes, backfill scripts): two copies
    merging the same day file at once could drop rows. `with history_lock(wait=...)`
    sets .ok; a lock older than LOCK_STALE_SEC (a crashed run) is taken over."""

    def __init__(self, wait: float = 3600, path: str | None = None):
        self.wait, self.path, self.ok = wait, path or LOCK_PATH, False

    def __enter__(self):
        deadline = time.time() + self.wait
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.ok = True
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > LOCK_STALE_SEC:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue
                if time.time() >= deadline:
                    return self
                time.sleep(5)

    def __exit__(self, *exc):
        if self.ok:
            try:
                os.remove(self.path)
            except OSError:
                pass


def day_range_ms(day: date) -> tuple:
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=ET)
    return int(start.timestamp() * 1000), int(start.timestamp() * 1000) + 86_400_000
