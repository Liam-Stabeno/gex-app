"""
background.py — Background loops and streamer callbacks.

All functions that run in background threads or are called by the WebSocket
streamer. Initialized at startup via init() to receive shared state references
from dashboard.py without circular imports.

Public API:
    init(...)             — inject shared state at startup
    refresh_gex(symbol)   — fetch and cache one GEX symbol
    refresh_price(symbol) — fetch and cache price candles for one symbol
    gex_loop()            — runs in a daemon thread
    price_loop()          — runs in a daemon thread
    on_streamer_candle(c) — WebSocket candle callback
    on_flow_alert(a)      — WebSocket flow alert callback
"""

import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from gex import get_access_token, fetch_option_chain, parse_gex, find_key_levels, get_watch_contracts
from price_history import fetch_candles, append_candles, load_candles
import delta_flow
import flow_alerts
import sse

ET = ZoneInfo('America/New_York')

# ── Shared state — injected via init() ───────────────────────────────────────

_cache          = None
_candle_cache   = None
_cache_lock     = None
_csv_lock       = None
_streamer_ref   = None   # list of length 1 so we can mutate from outside
_gex_watch      = None
_gex_watch_lock = None
_symbols        = None
_price_symbols  = None
_refresh_interval      = 60
_price_sync_interval   = 60


def init(cache, candle_cache, cache_lock, csv_lock,
         streamer_ref, gex_watch, gex_watch_lock,
         symbols, price_symbols,
         refresh_interval=60, price_sync_interval=60):
    """Inject shared state. Call once at startup before starting threads."""
    global _cache, _candle_cache, _cache_lock, _csv_lock
    global _streamer_ref, _gex_watch, _gex_watch_lock
    global _symbols, _price_symbols
    global _refresh_interval, _price_sync_interval

    _cache               = cache
    _candle_cache        = candle_cache
    _cache_lock          = cache_lock
    _csv_lock            = csv_lock
    _streamer_ref        = streamer_ref
    _gex_watch           = gex_watch
    _gex_watch_lock      = gex_watch_lock
    _symbols             = symbols
    _price_symbols       = price_symbols
    _refresh_interval    = refresh_interval
    _price_sync_interval = price_sync_interval


# ── GEX ──────────────────────────────────────────────────────────────────────

def _gex_to_dict(gex_df, spot):
    """Filter to significant strikes and serialize to lists."""
    if gex_df.empty:
        return {'strikes': [], 'net_gex': []}
    max_abs = gex_df['net_gex'].abs().max()
    if max_abs > 0:
        threshold = max_abs * 0.02
        gex_df = gex_df[gex_df['net_gex'].abs() >= threshold]
    gex_df = gex_df.sort_values('strike')
    return {
        'strikes': gex_df['strike'].tolist(),
        'net_gex': gex_df['net_gex'].tolist(),
    }


def refresh_gex(symbol: str):
    try:
        token        = get_access_token()
        strike_count = 150 if symbol in ('$SPX', 'SPX') else 200
        chain        = fetch_option_chain(symbol, token, strike_count=strike_count)
        gex_all, gex_0dte, gex_multi, spot, _ = parse_gex(chain)
        levels       = find_key_levels(gex_all, spot)
        total_gex    = float(gex_all['net_gex'].sum())

        levels_multi = find_key_levels(gex_multi, spot) if not gex_multi.empty else levels
        levels_0dte  = find_key_levels(gex_0dte,  spot) if not gex_0dte.empty  else {}

        def serialize_levels(lvl):
            return {k: (float(v) if v is not None else None) for k, v in lvl.items()}

        odte_date = None
        for exp_key in chain.get('callExpDateMap', {}).keys():
            if exp_key.endswith(':0'):
                odte_date = exp_key.split(':')[0]
                break

        data = {
            'symbol':       symbol.replace('$', '').replace('/', ''),
            'spot':         spot,
            'total_gex':    total_gex,
            'regime':       'POSITIVE' if total_gex > 0 else 'NEGATIVE',
            'levels_multi': serialize_levels(levels_multi),
            'levels_0dte':  serialize_levels(levels_0dte),
            'multi':        _gex_to_dict(gex_multi, spot),
            'zero':         _gex_to_dict(gex_0dte,  spot),
            'has_0dte':     not gex_0dte.empty,
            'odte_date':    odte_date,
            'updated':      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

        display_sym = symbol.replace('$', '').replace('/', '')
        call_wall   = levels_multi.get('call_wall')
        watch       = get_watch_contracts(chain, call_wall, underlying=display_sym)

        with _cache_lock:
            _cache[symbol] = data

        if display_sym == 'SPX':
            new_snap = delta_flow.extract_chain_snapshot(chain)
            delta_flow.process_chain_snapshot(new_snap)

            with _gex_watch_lock:
                _gex_watch['SPX'] = watch
                all_contracts = list(_gex_watch.get('SPX', []))

            streamer = _streamer_ref[0] if _streamer_ref else None
            if all_contracts and streamer:
                n_0dte       = sum(1 for c in all_contracts if c.get('is_0dte'))
                n_multi      = len(all_contracts) - n_0dte
                strikes_0dte = sorted(set(c['strike'] for c in all_contracts if c.get('is_0dte')))
                ts           = datetime.now().strftime('%H:%M:%S')
                print(f'[{ts}] Watch list: {len(all_contracts)} contracts  '
                      f'(0DTE={n_0dte}  MULTI={n_multi})  '
                      f'0DTE range {int(strikes_0dte[0]) if strikes_0dte else "?"}'
                      f'–{int(strikes_0dte[-1]) if strikes_0dte else "?"}')
                streamer.update_options_watch(all_contracts)

        gex_b      = total_gex / 1e9
        regime_str = 'POS' if total_gex > 0 else 'NEG'
        flip       = levels_multi.get('flip_level')
        pw         = levels_multi.get('put_wall')
        cw         = levels_multi.get('call_wall')
        pin        = levels_multi.get('pin')
        gex_0dte_b = float(gex_0dte['net_gex'].sum()) / 1e9 if not gex_0dte.empty else 0.0
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] GEX  {display_sym:4s}"
            f"  spot={spot:>8.2f}"
            f"  total={gex_b:+.2f}B ({regime_str})"
            f"  0dte={gex_0dte_b:+.2f}B"
            f"  flip={flip}  pw={pw}  cw={cw}  pin={pin}"
            f"  |  {len(watch)} contracts watched"
        )

    except Exception as e:
        print(f"[ERROR] GEX {symbol}: {e}")


# ── Price ─────────────────────────────────────────────────────────────────────

def refresh_price(symbol: str):
    try:
        token       = get_access_token()
        total_added = 0

        historical = fetch_candles(symbol, token, days=2, frequency=1)
        if historical:
            with _csv_lock:
                added = append_candles(symbol, historical)
            total_added += added

        today_midnight = datetime.now(tz=ET).replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(today_midnight.timestamp() * 1000)
        end_ms   = int((time.time() + 3600) * 1000)
        live = fetch_candles(symbol, token, frequency=1, start_ms=start_ms, end_ms=end_ms)
        if live:
            with _csv_lock:
                added = append_candles(symbol, live)
            total_added += added
            if added > 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Price live: {symbol} +{added} candles today")

        if total_added > 0:
            candles = load_candles(symbol)
            with _cache_lock:
                _candle_cache[symbol] = candles
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Price updated: {symbol} +{total_added} total")

    except Exception as e:
        print(f"[ERROR] Price {symbol}: {e}")


# ── Background loops ──────────────────────────────────────────────────────────

def gex_loop():
    while True:
        for symbol in _symbols:
            refresh_gex(symbol)
            time.sleep(2)
        time.sleep(_refresh_interval)


def price_loop():
    while True:
        time.sleep(_price_sync_interval)
        for symbol in _price_symbols:
            refresh_price(symbol)
            time.sleep(2)


# ── Streamer callbacks ────────────────────────────────────────────────────────

def on_streamer_candle(candle: dict):
    """
    Called by SchwabStreamer on each incoming candle update.
    Updates candle_cache, pushes SSE, persists completed bars to CSV.
    """
    raw_symbol = candle['symbol']
    ts_ms      = candle['datetime']
    is_final   = candle.get('is_final', False)

    push_sse  = False
    write_csv = False
    with _cache_lock:
        existing = _candle_cache.get(raw_symbol, [])
        if existing:
            last_ts = existing[-1]['datetime']
            if ts_ms == last_ts:
                existing[-1] = candle
                push_sse = True
            elif ts_ms > last_ts:
                existing.append(candle)
                push_sse  = True
                write_csv = is_final
        else:
            existing.append(candle)
            push_sse  = True
            write_csv = is_final
        _candle_cache[raw_symbol] = existing

    if not push_sse:
        return

    if write_csv:
        csv_candle = {k: v for k, v in candle.items() if k not in ('is_final', 'symbol')}
        try:
            with _csv_lock:
                append_candles(raw_symbol, [csv_candle])
        except Exception as e:
            print(f'[ERROR] CSV write {raw_symbol}: {e}')

    display_sym = raw_symbol.replace('$', '').replace('/', '')
    sse.push({
        'type':   'candle',
        'symbol':  display_sym,
        'time':    ts_ms // 1000,
        'open':    candle['open'],
        'high':    candle['high'],
        'low':     candle['low'],
        'close':   candle['close'],
        'volume':  candle['volume'],
    })

    ts   = datetime.now().strftime('%H:%M:%S')
    flag = ' [saved]' if write_csv else ''
    print(f'[{ts}] Streamer candle: {raw_symbol} {candle["close"]:.2f}{flag}')


def on_flow_alert(alert: dict):
    """Called by SchwabStreamer when a weighted options volume spike is detected."""
    alert['time'] = int(datetime.now().timestamp())
    sse.push({'type': 'flow_alert', **alert})
    delta_flow.record_alert(alert)
    flow_alerts.append(alert)
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] FLOW  {alert['underlying']} {alert['strike']} "
          f"{alert['side'].upper()} {alert['expiry_label']}  "
          f"+{alert['volume_delta']:,} contracts  {alert.get('direction','?')}")
