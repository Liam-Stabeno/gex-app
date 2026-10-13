"""
market_history.py — as much price history as Schwab gives for SPX, ES, the volatility
indexes and internals, merged into data/price_history_<SYM>[_<interval>].csv
(deduped, finished bars only, kept forever):

  1-minute  ~10 trading days (Schwab's limit)   price_history_<SYM>.csv      (not SPX/ES:
            the running app owns those files and keeps them current itself)
  5-minute  ~9 months                            price_history_<SYM>_5m.csv
  daily     ~20 years                            price_history_<SYM>_1d.csv

backfill() pulls everything (tools/backfill_all.py); refresh_recent() tops it up after
each close (background.option_history_loop).
"""
import time
from datetime import datetime, timedelta

import requests

import price_history as ph
from gex import SCHWAB_TIMEOUT, get_access_token

SYMBOLS = ['$SPX', '/ES', '$VIX', '$VIX9D', '$VIX3M', '$VVIX', '$TICK', '$TRIN', 'SPY', 'QQQ', '$NDX', '$RUT']
APP_OWNED_1M = {'$SPX', '/ES'}          # price_history_SPX/ES.csv: written by the running app


def _get(symbol: str, params: dict, token: str) -> list:
    for _ in range(3):
        r = requests.get('https://api.schwabapi.com/marketdata/v1/pricehistory', timeout=SCHWAB_TIMEOUT,
                         headers={'Authorization': f'Bearer {token}'},
                         params={'symbol': symbol, 'needExtendedHoursData': True, **params})
        if r.status_code == 429:
            time.sleep(60)
            continue
        if not r.ok:
            return []
        return [{k: c[k] for k in ph.CANDLE_FIELDS} for c in r.json().get('candles', []) or []]
    return []


def _save(symbol: str, candles: list, interval: str, interval_ms: int) -> int:
    done = ph.finished(candles, interval_ms)            # never a bar still in progress
    return len(ph.append_new(symbol, done, interval)) if done else 0


def pull_symbol(symbol: str, token: str, minute_days: int = 60, five_min_days: int = 280,
                daily_years: int = 20, one_minute: bool = True) -> dict:
    now = datetime.now()
    end = int(now.timestamp() * 1000)
    out = {}
    if one_minute and symbol not in APP_OWNED_1M:
        c = _get(symbol, {'frequencyType': 'minute', 'frequency': 1,
                          'startDate': int((now - timedelta(days=minute_days)).timestamp() * 1000),
                          'endDate': end}, token)
        out['1m'] = _save(symbol, c, '1m', 60_000)
    c = _get(symbol, {'frequencyType': 'minute', 'frequency': 5,
                      'startDate': int((now - timedelta(days=five_min_days)).timestamp() * 1000),
                      'endDate': end}, token)
    out['5m'] = _save(symbol, c, '5m', 300_000)
    c = _get(symbol, {'periodType': 'year', 'period': daily_years, 'frequencyType': 'daily', 'frequency': 1}, token)
    out['1d'] = _save(symbol, c, '1d', 86_400_000)
    return out


def backfill(symbols=None, pause: float = 1.0) -> dict:
    """Everything Schwab has, for every symbol. Returns {symbol: {interval: rows added}}."""
    token, res = get_access_token(), {}
    for s in symbols or SYMBOLS:
        res[s] = pull_symbol(s, token)
        print(f'[market_history] {s}: {res[s]}', flush=True)
        time.sleep(pause)
    return res


def refresh_recent(symbols=None, pause: float = 1.0) -> dict:
    """After the close: the last few days at every interval (1-minute only for symbols
    the app doesn't already record)."""
    token, res = get_access_token(), {}
    for s in symbols or SYMBOLS:
        res[s] = pull_symbol(s, token, minute_days=4, five_min_days=4, daily_years=1)
        time.sleep(pause)
    return res
