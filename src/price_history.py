import os
import csv
import requests
from datetime import datetime, timezone

DATA_DIR = 'data'
CANDLE_FIELDS = ['datetime', 'open', 'high', 'low', 'close', 'volume']


def csv_path(symbol: str) -> str:
    safe = symbol.replace('$', '').replace('/', '')
    return os.path.join(DATA_DIR, f'price_history_{safe}.csv')


def load_candles(symbol: str) -> list:
    """Load all candles from CSV on disk."""
    path = csv_path(symbol)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                'datetime': int(row['datetime']),
                'open':     float(row['open']),
                'high':     float(row['high']),
                'low':      float(row['low']),
                'close':    float(row['close']),
                'volume':   int(row['volume'])
            })
    return rows


def save_candles(symbol: str, candles: list):
    """Write full candle list to CSV, replacing existing file."""
    path = csv_path(symbol)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CANDLE_FIELDS)
        writer.writeheader()
        writer.writerows(candles)


def append_candles(symbol: str, candles: list):
    """Append new candles to existing CSV, skipping duplicates by datetime."""
    path = csv_path(symbol)
    existing = load_candles(symbol)
    existing_dts = {c['datetime'] for c in existing}

    new_candles = [c for c in candles if c['datetime'] not in existing_dts]
    if not new_candles:
        return 0

    file_exists = os.path.exists(path)
    with open(path, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CANDLE_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_candles)

    return len(new_candles)


def fetch_candles(symbol: str, token: str, days: int = 1, frequency: int = 1) -> list:
    """Fetch OHLCV candles from Schwab price history API."""
    response = requests.get(
        'https://api.schwabapi.com/marketdata/v1/pricehistory',
        headers={'Authorization': f'Bearer {token}'},
        params={
            'symbol': symbol,
            'periodType': 'day',
            'period': days,
            'frequencyType': 'minute',
            'frequency': frequency,
            'needExtendedHoursData': True
        }
    )

    if not response.ok:
        print(f"[price_history] Failed to fetch {symbol}: {response.text}")
        return []

    data = response.json()
    candles = data.get('candles', [])

    # Normalize to our format
    return [{
        'datetime': c['datetime'],
        'open':     c['open'],
        'high':     c['high'],
        'low':      c['low'],
        'close':    c['close'],
        'volume':   c['volume']
    } for c in candles]


def format_time(dt_ms: int) -> str:
    """Convert millisecond timestamp to HH:MM string for display."""
    return datetime.fromtimestamp(dt_ms / 1000).strftime('%H:%M')


def sync_symbol(symbol: str, token: str) -> list:
    """
    Load existing candles from disk, detect gap since last candle,
    fetch missing data from Schwab, merge and save.
    Returns full candle list.
    """
    existing = load_candles(symbol)

    # Work out how many days back we need to fetch
    if existing:
        last_ts = existing[-1]['datetime'] / 1000
        last_dt = datetime.fromtimestamp(last_ts)
        gap_days = (datetime.now() - last_dt).days + 1  # +1 to include partial day
        gap_days = min(gap_days, 10)  # Schwab max is 10 days
        if gap_days < 1:
            gap_days = 1
        print(f"[price_history] {symbol}: last candle {last_dt.strftime('%Y-%m-%d %H:%M')}, fetching {gap_days} day(s) to fill gap")
    else:
        gap_days = 10  # no existing data — pull maximum
        print(f"[price_history] {symbol}: no existing data, fetching {gap_days} days")

    fresh = fetch_candles(symbol, token, days=gap_days)

    if not fresh:
        print(f"[price_history] No candles returned for {symbol}")
        return existing

    added = append_candles(symbol, fresh)
    print(f"[price_history] {symbol}: {len(existing)} existing + {added} new candles added")

    return load_candles(symbol)


def backfill(symbol: str, token: str, days: int = 35):
    """Pull maximum available 1-minute history and save to CSV."""
    print(f"\nBackfilling {symbol} ({days} days of 1-minute candles)...")
    candles = fetch_candles(symbol, token, days=days, frequency=1)
    if not candles:
        print(f"  No data returned for {symbol}")
        return

    # Deduplicate by datetime
    seen = {}
    for c in candles:
        seen[c['datetime']] = c
    unique = sorted(seen.values(), key=lambda x: x['datetime'])

    save_candles(symbol, unique)

    first = unique[0]
    last = unique[-1]
    first_dt = datetime.fromtimestamp(first['datetime'] / 1000).strftime('%Y-%m-%d %H:%M')
    last_dt = datetime.fromtimestamp(last['datetime'] / 1000).strftime('%Y-%m-%d %H:%M')
    print(f"  Saved {len(unique)} candles")
    print(f"  From: {first_dt}")
    print(f"  To:   {last_dt}")
    print(f"  Last close: {last['close']}")


if __name__ == '__main__':
    from gex import get_access_token
    token = get_access_token()

    # Backfill all supported symbols (no futures — Schwab doesn't support /ES history)
    BACKFILL_SYMBOLS = ['SPY', 'QQQ', '$SPX', '$NDX']

    for sym in BACKFILL_SYMBOLS:
        backfill(sym, token, days=10)

    print("\nDone. Check data/ folder for CSV files.")
