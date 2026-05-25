"""
backfill_massive.py — Pull historical 1-minute OHLCV from massive.com and
merge into the existing price_history CSVs.

Supports:
  - Stocks  (SPY, QQQ)   → /v2/aggs/ticker/{ticker}/range/1/minute/{from}/{to}
  - Indices ($SPX)       → same endpoint, ticker = I:SPX
  - Futures (/ES)        → /futures/v1/aggs/{contract}  (requires futures plan)

Free-tier typically gives 2 years of history for stocks/indices.
Futures require a separate Futures plan.

Usage:
  python src/backfill_massive.py                        # all symbols, full history
  python src/backfill_massive.py SPY QQQ                # specific symbols
  python src/backfill_massive.py --from 2024-01-01      # custom start date
  python src/backfill_massive.py --dry-run              # show counts, don't write

Set your API key via env var or edit API_KEY below:
  set MASSIVE_API_KEY=your_key_here
"""

import os
import sys
import time
import argparse
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
from price_history import append_candles, load_candles

# ── Config ──────────────────────────────────────────────────────────────────────

API_KEY       = os.environ.get('MASSIVE_API_KEY', 'ATaoBHrT7lahZdVt_mNdXX5rsCcUxaSk')
BASE_URL      = 'https://api.massive.com'
ET            = ZoneInfo('America/New_York')
RATE_LIMIT_S  = 13   # free tier: 5 calls/min → 1 call every 12s (13s for safety)

# Maps app symbol → (massive ticker, asset class)
# asset class: 'stock' | 'index' | 'futures'
SYMBOL_MAP = {
    'SPY':  ('SPY',   'stock'),
    'QQQ':  ('QQQ',   'stock'),
    '$SPX': ('I:SPX', 'index'),
    '/ES':  (None,    'futures'),   # handled separately — needs contract chain
}

# ES front-month contract chain (quarterly: Mar=H, Jun=M, Sep=U, Dec=Z)
# Ticker format: single-digit year suffix (e.g. ESM4 = June 2024, ESU5 = Sep 2025)
# Roll ~2 weeks before expiration (3rd Friday of contract month)
ES_CONTRACTS = [
    ('ESH4', '2024-01-01', '2024-03-14'),
    ('ESM4', '2024-03-15', '2024-06-13'),
    ('ESU4', '2024-06-14', '2024-09-19'),
    ('ESZ4', '2024-09-20', '2024-12-19'),
    ('ESH5', '2024-12-20', '2025-03-20'),
    ('ESM5', '2025-03-21', '2025-06-19'),
    ('ESU5', '2025-06-20', '2025-09-18'),
    ('ESZ5', '2025-09-19', '2025-12-18'),
    ('ESH6', '2025-12-19', '2026-03-19'),
    ('ESM6', '2026-03-20', '2099-12-31'),  # current front month — open-ended
]


# ── HTTP helpers ─────────────────────────────────────────────────────────────────

def _get(url: str, params: dict = None) -> dict:
    """GET with retries and rate-limit backoff."""
    if params is None:
        params = {}
    params['apiKey'] = API_KEY

    for attempt in range(5):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                wait = 2 ** attempt
                print(f"    Rate limited — waiting {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code == 403:
                print(f"    403 Forbidden — check your API key or plan access.")
                return {}
            if not r.ok:
                print(f"    HTTP {r.status_code}: {r.text[:200]}")
                return {}
            return r.json()
        except requests.RequestException as e:
            print(f"    Request error: {e}")
            time.sleep(2 ** attempt)
    return {}


# ── Stock / Index fetch (shared endpoint) ────────────────────────────────────────

def fetch_stock_bars(ticker: str, from_date: str, to_date: str,
                     interval_minutes: int = 1) -> list:
    """
    Fetch 1-min (or N-min) OHLCV bars from the stocks/indices aggregate endpoint.
    Follows next_url pagination automatically.
    Returns list of candle dicts with datetime in milliseconds.
    """
    url = f'{BASE_URL}/v2/aggs/ticker/{ticker}/range/{interval_minutes}/minute/{from_date}/{to_date}'
    params = {'adjusted': 'true', 'sort': 'asc', 'limit': 50000}

    all_candles = []
    page = 0

    while url:
        page += 1
        data = _get(url, params if page == 1 else {})
        if not data:
            break

        status = data.get('status', '')
        if status not in ('OK', 'DELAYED'):
            print(f"    API status: {status}")
            break

        results = data.get('results', [])
        for r in results:
            all_candles.append({
                'datetime': int(r['t']),
                'open':     round(float(r['o']), 4),
                'high':     round(float(r['h']), 4),
                'low':      round(float(r['l']), 4),
                'close':    round(float(r['c']), 4),
                'volume':   int(r.get('v', 0)),
            })

        url = data.get('next_url')   # None when last page
        params = {}                  # next_url already has all params encoded
        if url:
            print(f"    Page {page} done ({len(all_candles)} total) — waiting {RATE_LIMIT_S}s...")
            time.sleep(RATE_LIMIT_S)

    return all_candles


# ── Futures fetch ────────────────────────────────────────────────────────────────

def fetch_futures_bars(contract: str, from_date: str, to_date: str) -> list:
    """
    Fetch 1-min bars for a specific ES futures contract.
    Returns candle dicts with datetime in milliseconds.
    Note: window_start in futures API is nanoseconds → convert to ms.
    """
    url = f'{BASE_URL}/futures/v1/aggs/{contract}'
    params = {
        'resolution':          '1min',
        'window_start.gte':    from_date,
        'window_start.lte':    to_date,
        'sort':                'window_start.asc',
        'limit':               50000,
    }

    all_candles = []
    page = 0

    while url:
        page += 1
        data = _get(url, params if page == 1 else {})
        if not data:
            break

        results = data.get('results', [])
        for r in results:
            # window_start is nanoseconds — convert to milliseconds
            ts_ns = r['window_start']
            ts_ms = ts_ns // 1_000_000

            all_candles.append({
                'datetime': ts_ms,
                'open':     round(float(r['open']),  4),
                'high':     round(float(r['high']),  4),
                'low':      round(float(r['low']),   4),
                'close':    round(float(r['close']), 4),
                'volume':   int(r.get('volume', 0)),
            })

        url = data.get('next_url')
        params = {}
        if url:
            print(f"    Page {page} done ({len(all_candles)} total) — waiting {RATE_LIMIT_S}s...")
            time.sleep(RATE_LIMIT_S)

    return all_candles


# ── Per-symbol backfill ──────────────────────────────────────────────────────────

def backfill_symbol(app_symbol: str, from_date: str, to_date: str,
                    dry_run: bool = False) -> int:
    """
    Fetch and merge all available 1-min bars for one symbol.
    Returns total candles added.
    """
    massive_ticker, asset_class = SYMBOL_MAP[app_symbol]

    print(f"\n{'--' * 30}")
    print(f"  {app_symbol}  [{asset_class}]  {from_date} -> {to_date}")

    existing = load_candles(app_symbol, interval='1m')
    print(f"  Existing 1m candles on disk: {len(existing)}")

    total_added = 0

    if asset_class in ('stock', 'index'):
        print(f"  Fetching from massive.com ({massive_ticker})...", flush=True)
        candles = fetch_stock_bars(massive_ticker, from_date, to_date)
        print(f"  {len(candles)} candles returned")

        if candles and not dry_run:
            added = append_candles(app_symbol, candles, interval='1m')
            print(f"  -> Added {added} new candles")
            total_added = added
        elif candles and dry_run:
            existing_dts = {c['datetime'] for c in existing}
            new_count = sum(1 for c in candles if c['datetime'] not in existing_dts)
            print(f"  [dry-run] Would add {new_count} new candles")
            total_added = new_count

    elif asset_class == 'futures':
        # Stitch together ES contracts for the requested date range
        req_from = date.fromisoformat(from_date)
        req_to   = date.fromisoformat(to_date)

        for contract, c_from_str, c_to_str in ES_CONTRACTS:
            c_from = date.fromisoformat(c_from_str)
            c_to   = date.fromisoformat(min(c_to_str, '2099-12-31'))

            # Clip contract range to requested range
            seg_from = max(req_from, c_from)
            seg_to   = min(req_to,   c_to)

            if seg_from > seg_to:
                continue   # no overlap

            print(f"  Contract {contract}: {seg_from} -> {seg_to}...", end=' ', flush=True)
            candles = fetch_futures_bars(contract, str(seg_from), str(seg_to))
            print(f"{len(candles)} candles")

            if candles and not dry_run:
                added = append_candles(app_symbol, candles, interval='1m')
                print(f"    -> Added {added} new candles")
                total_added += added
            elif candles and dry_run:
                existing_dts = {c['datetime'] for c in existing}
                new_count = sum(1 for c in candles if c['datetime'] not in existing_dts)
                print(f"    [dry-run] Would add {new_count} new candles")
                total_added += new_count

            time.sleep(RATE_LIMIT_S)

    return total_added


# ── Main ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Backfill price_history CSVs from massive.com (1-min bars).'
    )
    parser.add_argument('symbols', nargs='*',
                        help='Symbols to backfill (default: SPY QQQ $SPX). '
                             'Add /ES if you have a futures plan.')
    parser.add_argument('--from', dest='from_date', default=None,
                        help='Start date YYYY-MM-DD (default: 2 years ago)')
    parser.add_argument('--to',   dest='to_date',   default=None,
                        help='End date YYYY-MM-DD (default: today)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be added without writing')
    args = parser.parse_args()

    today     = date.today()
    from_date = args.from_date or str(today - timedelta(days=730))  # 2 years
    to_date   = args.to_date   or str(today)

    # Default: all symbols (free tier includes stocks, indices, and futures)
    default_symbols = ['SPY', 'QQQ', '$SPX', '/ES']
    symbols = args.symbols if args.symbols else default_symbols

    # Normalise symbol input (accept SPX → $SPX, ES → /ES)
    normalised = []
    for s in symbols:
        if s in SYMBOL_MAP:
            normalised.append(s)
        elif f'${s}' in SYMBOL_MAP:
            normalised.append(f'${s}')
        elif f'/{s}' in SYMBOL_MAP:
            normalised.append(f'/{s}')
        else:
            print(f"WARNING: Unknown symbol '{s}' — skipping")
    symbols = normalised

    print(f"\n{'=' * 60}")
    print(f"  massive.com Backfill  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Symbols   : {symbols}")
    print(f"  From      : {from_date}")
    print(f"  To        : {to_date}")
    print(f"  Dry run   : {args.dry_run}")
    print(f"{'=' * 60}")

    # Estimate time upfront so user knows what to expect
    # At 50k candles/page and ~390 1m bars/day × 250 days × 2yrs ≈ 195k bars/symbol
    # → ~4 pages/symbol × 13s = ~52s/symbol. With 3 symbols ≈ 2-3 minutes total.
    print(f"\n  Note: free tier = 5 calls/min. Expect ~{len(symbols) * 55}s total.\n")

    totals = {}
    for sym in symbols:
        added = backfill_symbol(sym, from_date, to_date, dry_run=args.dry_run)
        totals[sym] = added
        time.sleep(RATE_LIMIT_S)   # pause between symbols

    print(f"\n{'=' * 60}")
    print("  SUMMARY")
    for sym, added in totals.items():
        print(f"  {sym:6s}  +{added} candles")
    grand = sum(totals.values())
    print(f"  {'TOTAL':6s}  +{grand} candles")
    print(f"{'=' * 60}\n")

    if args.dry_run:
        print("  Dry run — no files written.\n")


if __name__ == '__main__':
    main()
