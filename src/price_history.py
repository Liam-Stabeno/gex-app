import os
import csv
import requests
from datetime import datetime, timezone, timedelta, time as dtime
from collections import defaultdict
from zoneinfo import ZoneInfo

DATA_DIR = 'data'
ET = ZoneInfo('America/New_York')
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

    if existing:
        last_ts = existing[-1]['datetime'] / 1000
        last_dt = datetime.fromtimestamp(last_ts)
        gap_days = (datetime.now() - last_dt).days + 1
        gap_days = min(gap_days, 10)
        gap_days = max(gap_days, 2)
        print(f"[price_history] {symbol}: last candle {last_dt.strftime('%Y-%m-%d %H:%M')}, fetching {gap_days} day(s) to fill gap")
    else:
        gap_days = 10
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


# Gap detection and filling

SCHWAB_MAX_DAYS = 10
MIN_SESSION_CANDLES = 370


def fetch_candles_range(symbol: str, token: str, start_ms: int, end_ms: int) -> list:
    """Fetch 1-min candles for a specific date range using startDate/endDate epoch ms."""
    response = requests.get(
        'https://api.schwabapi.com/marketdata/v1/pricehistory',
        headers={'Authorization': f'Bearer {token}'},
        params={
            'symbol':               symbol,
            'frequencyType':        'minute',
            'frequency':            1,
            'startDate':            start_ms,
            'endDate':              end_ms,
            'needExtendedHoursData': True,
        }
    )
    if not response.ok:
        print(f"  [fetch_range] {symbol} {response.status_code}: {response.text}")
        return []
    data = response.json()
    return [{
        'datetime': c['datetime'],
        'open':     c['open'],
        'high':     c['high'],
        'low':      c['low'],
        'close':    c['close'],
        'volume':   c['volume'],
    } for c in data.get('candles', [])]


def find_gaps(symbol: str) -> list:
    """
    Scan the symbol's CSV for missing or incomplete trading sessions.

    For equities: checks each weekday from first candle date to today for
    sessions with fewer than MIN_SESSION_CANDLES market-hours candles.

    For /ES futures: checks for time gaps > 70 min between consecutive candles.

    Returns list of dicts: { date, candle_count, missing, fillable }
    fillable = True if the gap is within SCHWAB_MAX_DAYS of today.
    """
    candles = load_candles(symbol)
    if not candles:
        print(f"  {symbol}: no CSV data found")
        return []

    is_futures = symbol in ('/ES', 'ES')
    today = datetime.now(tz=ET).date()
    cutoff = today - timedelta(days=SCHWAB_MAX_DAYS)

    gaps = []

    if is_futures:
        for i in range(1, len(candles)):
            prev_dt = datetime.fromtimestamp(candles[i-1]['datetime'] / 1000, tz=ET)
            curr_dt = datetime.fromtimestamp(candles[i  ]['datetime'] / 1000, tz=ET)
            gap_min = (curr_dt - prev_dt).total_seconds() / 60
            if gap_min > 70 and curr_dt.weekday() < 5:
                gaps.append({
                    'date':         prev_dt.date(),
                    'gap_start':    prev_dt,
                    'gap_end':      curr_dt,
                    'gap_minutes':  round(gap_min),
                    'fillable':     prev_dt.date() >= cutoff,
                })
    else:
        # Group market-hours candles (9:30-16:00 ET) by trading date
        by_date = defaultdict(int)
        for c in candles:
            dt = datetime.fromtimestamp(c['datetime'] / 1000, tz=ET)
            t  = dt.time()
            if dt.weekday() < 5 and dtime(9, 30) <= t <= dtime(16, 0):
                by_date[dt.date()] += 1

        # Walk every weekday from first candle date through today
        first_date = datetime.fromtimestamp(candles[0]['datetime'] / 1000, tz=ET).date()

        d = first_date
        while d <= today:
            if d.weekday() < 5:
                count = by_date.get(d, 0)
                if count < MIN_SESSION_CANDLES:
                    gaps.append({
                        'date':         d,
                        'candle_count': count,
                        'missing':      MIN_SESSION_CANDLES - count,
                        'fillable':     d >= cutoff,
                    })
            d += timedelta(days=1)

    return gaps


def fill_gaps(symbol: str, token: str, dry_run: bool = False) -> int:
    """
    Find and fill all gaps in a symbol's CSV within Schwab's 10-day window.
    Returns the total number of new candles added.
    """
    print(f"\n{'--'*25}")
    print(f"Checking {symbol} for gaps...")
    gaps = find_gaps(symbol)

    if not gaps:
        print(f"  No gaps found")
        return 0

    fillable   = [g for g in gaps if g['fillable']]
    unfillable = [g for g in gaps if not g['fillable']]

    if unfillable:
        print(f"  WARNING: {len(unfillable)} old gap(s) (>{SCHWAB_MAX_DAYS} days) -- cannot fill:")
        for g in unfillable:
            print(f"    {g['date']}  ({g.get('candle_count', '?')} candles)")

    if not fillable:
        print(f"  No fillable gaps within the last {SCHWAB_MAX_DAYS} days")
        return 0

    print(f"  Found {len(fillable)} fillable gap(s):")
    for g in fillable:
        if 'gap_minutes' in g:
            print(f"    {g['date']}  gap of {g['gap_minutes']} min  ({g['gap_start'].strftime('%H:%M')}-{g['gap_end'].strftime('%H:%M')} ET)")
        else:
            print(f"    {g['date']}  {g['candle_count']} candles  ({g['missing']} missing)")

    if dry_run:
        print("  [dry_run] Skipping fetch.")
        return 0

    total_added = 0
    for g in fillable:
        date = g['date']
        start_dt = datetime(date.year, date.month, date.day,  0,  0, tzinfo=ET)
        end_dt   = datetime(date.year, date.month, date.day, 23, 59, tzinfo=ET)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms   = int(end_dt.timestamp()   * 1000)

        print(f"  Fetching {symbol} for {date}...", end=' ', flush=True)
        fresh = fetch_candles_range(symbol, token, start_ms, end_ms)
        if not fresh:
            print("no data returned")
            continue

        added = append_candles(symbol, fresh)
        print(f"+{added} candles")
        total_added += added

    print(f"  Total added: {total_added} candles")
    return total_added


def fill_all_gaps(token: str, symbols: list = None, dry_run: bool = False) -> dict:
    """
    Run fill_gaps for all symbols (or the provided list).
    Returns dict of { symbol: candles_added }.
    """
    if symbols is None:
        symbols = ['SPY', 'QQQ', '$SPX', '/ES']

    print(f"\n{'=='*25}")
    print(f"GAP FILL -- {datetime.now(tz=ET).strftime('%Y-%m-%d %H:%M ET')}")
    print(f"Symbols: {symbols}")
    print(f"{'=='*25}")

    results = {}
    for sym in symbols:
        added = fill_gaps(sym, token, dry_run=dry_run)
        results[sym] = added

    print(f"\n{'=='*25}")
    print("SUMMARY")
    for sym, added in results.items():
        print(f"  {sym:6s}  +{added} candles")
    print(f"{'=='*25}\n")
    return results


if __name__ == '__main__':
    import sys
    from gex import get_access_token
    token = get_access_token()

    args = sys.argv[1:]

    if '--fill-gaps' in args or '--fill' in args:
        dry = '--dry-run' in args
        FILL_SYMBOLS = ['SPY', 'QQQ', '$SPX', '/ES']
        fill_all_gaps(token, symbols=FILL_SYMBOLS, dry_run=dry)

    elif '--check-gaps' in args or '--check' in args:
        for sym in ['SPY', 'QQQ', '$SPX', '/ES']:
            gaps = find_gaps(sym)
            fillable = sum(1 for g in gaps if g['fillable'])
            print(f"\n{sym}: {len(gaps)} gap(s)  ({fillable} fillable)")
            for g in gaps:
                tag = '[OK]' if g['fillable'] else '[X] '
                if 'gap_minutes' in g:
                    print(f"  {tag} {g['date']}  {g['gap_minutes']} min gap")
                else:
                    print(f"  {tag} {g['date']}  {g['candle_count']} candles ({g['missing']} missing)")

    else:
        BACKFILL_SYMBOLS = ['SPY', 'QQQ', '$SPX', '/ES']
        for sym in BACKFILL_SYMBOLS:
            backfill(sym, token, days=10)
        print("\nDone. Check data/ folder for CSV files.")
