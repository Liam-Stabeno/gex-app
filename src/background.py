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

import csv
import os
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from gex import get_access_token, fetch_option_chain, parse_gex, find_key_levels, get_watch_contracts
from price_history import fetch_candles, append_candles, load_candles
import bs
import delta_flow
import flow_alerts
import sse

_SRC_DIR     = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SRC_DIR)
_DATA_DIR    = os.path.join(_PROJECT_DIR, 'data')

_GEX_SNAPSHOT_FIELDS  = ['timestamp', 'spot', 'total_gex', 'regime',
                          'flip_level', 'put_wall', 'call_wall', 'pin',
                          'strike_min', 'strike_max']
_WATCHLIST_FIELDS     = ['timestamp', 'symbol', 'strike', 'side',
                          'expiry_label', 'delta', 'oi']


def _append_gex_snapshot(sym: str, tag: str, row: dict):
    """Append one row to gex_{tag}_{sym}_{date}.csv, writing header if new."""
    date  = datetime.now().strftime('%Y-%m-%d')
    path  = os.path.join(_DATA_DIR, f'gex_{tag}_{sym}_{date}.csv')
    fields = _GEX_SNAPSHOT_FIELDS
    new_file = not os.path.exists(path)
    with open(path, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def _append_watchlist(sym: str, ts: str, contracts: list):
    """Append all watched contracts for this refresh to gex_watchlist_{sym}_{date}.csv."""
    date  = datetime.now().strftime('%Y-%m-%d')
    path  = os.path.join(_DATA_DIR, f'gex_watchlist_{sym}_{date}.csv')
    new_file = not os.path.exists(path)
    with open(path, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=_WATCHLIST_FIELDS)
        if new_file:
            writer.writeheader()
        for c in contracts:
            writer.writerow({
                'timestamp':    ts,
                'symbol':       c['symbol'],
                'strike':       c['strike'],
                'side':         c['side'],
                'expiry_label': c['expiry_label'],
                'delta':        c['delta'],
                'oi':           c['oi'],
            })

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

# ── Live GEX state ────────────────────────────────────────────────────────────
# Populated from chain (OI doesn't change tick-to-tick).
_oi_cache      = {}   # contract_symbol -> oi (int)
_contract_meta = {}   # contract_symbol -> {strike, side, expiry_date, is_0dte}
# Updated on every LEVELONE_OPTIONS tick from the streamer.
_quote_cache   = {}   # contract_symbol -> (bid, ask)
_gex_dirty     = threading.Event()   # set when any quote updates
_RISK_FREE     = 0.05                # annualised risk-free rate for BS


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
    """Serialize strikes to lists, trimmed to the active range.

    Active range = min/max strike where net_gex != 0.
    All strikes within that range are included (zeros too), so the
    chart shows a continuous bar span with no phantom gaps at the edges.
    """
    if gex_df.empty:
        return {'strikes': [], 'net_gex': [], 'strike_min': None, 'strike_max': None}
    gex_df = gex_df.sort_values('strike')
    active = gex_df[gex_df['net_gex'] != 0]
    if active.empty:
        return {'strikes': [], 'net_gex': [], 'strike_min': None, 'strike_max': None}
    lo = active['strike'].min()
    hi = active['strike'].max()
    trimmed = gex_df[(gex_df['strike'] >= lo) & (gex_df['strike'] <= hi)]
    return {
        'strikes':    trimmed['strike'].tolist(),
        'net_gex':    trimmed['net_gex'].tolist(),
        'strike_min': float(lo),
        'strike_max': float(hi),
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

        multi_dict = _gex_to_dict(gex_multi, spot)
        zero_dict  = _gex_to_dict(gex_0dte,  spot)

        data = {
            'symbol':       symbol.replace('$', '').replace('/', ''),
            'spot':         spot,
            'total_gex':    total_gex,
            'regime':       'POSITIVE' if total_gex > 0 else 'NEGATIVE',
            'levels_multi': serialize_levels(levels_multi),
            'levels_0dte':  serialize_levels(levels_0dte),
            'multi':        multi_dict,
            'zero':         zero_dict,
            'has_0dte':     not gex_0dte.empty,
            'odte_date':    odte_date,
            'updated':      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

        display_sym = symbol.replace('$', '').replace('/', '')
        call_wall   = levels_multi.get('call_wall')
        watch       = get_watch_contracts(chain, call_wall, underlying=display_sym)

        # ── Populate OI + contract meta for live GEX loop ───────────────────
        new_oi   = {c['symbol']: c['oi'] for c in watch}
        new_meta = {c['symbol']: {
            'strike':      c['strike'],
            'side':        c['side'],
            'expiry_date': c.get('expiry_date', ''),
            'is_0dte':     c.get('is_0dte', False),
        } for c in watch}
        _oi_cache.update(new_oi)
        _contract_meta.update(new_meta)

        # ── Detect post-market CLOSED state (OI zeroed after expiry) ────────
        all_oi_zero = all(c['oi'] == 0 for c in watch)
        if all_oi_zero:
            data['regime'] = 'CLOSED'

        with _cache_lock:
            _cache[symbol] = data

        # ── Persist snapshots ────────────────────────────────────────────────
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        def _lvl(lvl_dict, key):
            v = lvl_dict.get(key)
            return float(v) if v is not None else ''

        _append_gex_snapshot(display_sym, 'snapshots', {
            'timestamp':  ts,
            'spot':       spot,
            'total_gex':  total_gex,
            'regime':     'POSITIVE' if total_gex > 0 else 'NEGATIVE',
            'flip_level': _lvl(levels_multi, 'flip_level'),
            'put_wall':   _lvl(levels_multi, 'put_wall'),
            'call_wall':  _lvl(levels_multi, 'call_wall'),
            'pin':        _lvl(levels_multi, 'pin'),
            'strike_min': multi_dict.get('strike_min', ''),
            'strike_max': multi_dict.get('strike_max', ''),
        })

        if not gex_0dte.empty:
            total_0dte = float(gex_0dte['net_gex'].sum())
            _append_gex_snapshot(display_sym, '0dte_snapshots', {
                'timestamp':  ts,
                'spot':       spot,
                'total_gex':  total_0dte,
                'regime':     'POSITIVE' if total_0dte > 0 else 'NEGATIVE',
                'flip_level': _lvl(levels_0dte, 'flip_level'),
                'put_wall':   _lvl(levels_0dte, 'put_wall'),
                'call_wall':  _lvl(levels_0dte, 'call_wall'),
                'pin':        _lvl(levels_0dte, 'pin'),
                'strike_min': zero_dict.get('strike_min', ''),
                'strike_max': zero_dict.get('strike_max', ''),
            })

        _append_watchlist(display_sym, ts, watch)
        # ─────────────────────────────────────────────────────────────────────

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

            # Push the latest candle to the browser so it updates without a reload.
            # Map raw CSV symbol → browser chart key (same as on_streamer_candle).
            _SYM_MAP = {'/ES': 'ES', '$SPX': 'SPX', '$VIX.X': 'VIX'}
            browser_sym = _SYM_MAP.get(symbol, symbol)
            if candles:
                last = candles[-1]
                sse.push({
                    'type':     'candle',
                    'symbol':   browser_sym,
                    'datetime': last['datetime'],
                    'open':     last['open'],
                    'high':     last['high'],
                    'low':      last['low'],
                    'close':    last['close'],
                    'volume':   last.get('volume', 0),
                    'is_final': True,
                })

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


def on_streamer_candle(candle: dict):
    """
    Called on every WebSocket price tick (CHART_FUTURES / LEVELONE_FUTURES).
    Pushes the candle to all connected browsers via SSE so the current bar
    animates in real-time without waiting for the REST poll.

    Normalises streamer symbols to browser keys:
        /ES  → ES    (CHART_FUTURES / LEVELONE_FUTURES)
        $SPX → SPX   (LEVELONE_EQUITIES)
    """
    # Map streamer symbols → browser chart keys
    _SYM_MAP = {'/ES': 'ES', '$SPX': 'SPX', '$VIX': 'VIX'}
    raw_sym  = candle['symbol']
    browser_sym = _SYM_MAP.get(raw_sym, raw_sym)

    try:
        sse.push({
            'type':     'candle',
            'symbol':   browser_sym,
            'datetime': candle['datetime'],
            'open':     candle['open'],
            'high':     candle['high'],
            'low':      candle['low'],
            'close':    candle['close'],
            'volume':   candle.get('volume', 0),
            'is_final': candle.get('is_final', False),
        })
    except Exception as e:
        print(f'[ERROR] on_streamer_candle SSE push: {e}')



# ── Live GEX (BS gamma from streamer quotes) ──────────────────────────────────

def on_options_quote(quote: dict):
    """
    Called on every LEVELONE_OPTIONS bid/ask tick from the streamer.
    Caches the quote and marks GEX as dirty for recompute.
    """
    sym = quote['symbol']
    bid = quote.get('bid', 0.0)
    ask = quote.get('ask', 0.0)
    if ask > 0.0:
        _quote_cache[sym] = (bid, ask)
        _gex_dirty.set()


def live_gex_loop():
    """
    Recomputes GEX from live streamer bid/ask every ~2 s when quotes change.
    Replaces chain-gamma with BS gamma derived from the current bid/ask mid.
    """
    import pandas as pd
    from datetime import date

    MIN_INTERVAL = 2.0
    last_push    = 0.0

    while True:
        _gex_dirty.wait(timeout=MIN_INTERVAL)
        _gex_dirty.clear()

        now = time.time()
        if now - last_push < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - (now - last_push))

        # Need a valid spot price
        spot = 0.0
        with _cache_lock:
            spx_data = _cache.get('$SPX') or _cache.get('SPX')
            if spx_data:
                spot = spx_data.get('spot', 0.0)
        if spot <= 0.0:
            continue

        today = date.today()

        # Snapshot caches (avoid holding lock during BS computation)
        meta_snap  = dict(_contract_meta)
        oi_snap    = dict(_oi_cache)
        quote_snap = dict(_quote_cache)

        if not meta_snap or not quote_snap:
            continue

        # ── Compute per-contract GEX, charm, and vanna ───────────────────
        gex_all   = {}   # strike -> net_gex (signed: calls+, puts-)
        gex_0dte  = {}
        gex_multi = {}
        # For TRUE PIN: absolute dealer rehedging magnitude per greek per strike
        charm_abs = {}   # strike -> sum(|charm| * oi * 100 * spot)
        vanna_abs = {}   # strike -> sum(|vanna| * oi * 100 * spot)

        for sym, meta in meta_snap.items():
            bid, ask = quote_snap.get(sym, (0.0, 0.0))
            oi       = oi_snap.get(sym, 0)
            if oi == 0 or ask <= 0.0:
                continue

            expiry_str = meta.get('expiry_date', '')
            if not expiry_str:
                continue
            try:
                T = bs.dte_to_t(expiry_str, today.isoformat())
            except Exception:
                continue

            flag = 'c' if meta['side'] == 'call' else 'p'
            g, c, v = bs.greeks_from_mid(bid, ask, spot, meta['strike'], T, _RISK_FREE, flag)
            sign = 1 if meta['side'] == 'call' else -1
            k    = meta['strike']

            gex  = g * oi * 100 * spot * sign
            gex_all[k]  = gex_all.get(k, 0.0) + gex
            if meta.get('is_0dte'):
                gex_0dte[k]  = gex_0dte.get(k, 0.0) + gex
            else:
                gex_multi[k] = gex_multi.get(k, 0.0) + gex

            # Charm and vanna: absolute value — both calls and puts drive
            # rehedging flows toward the pinned strike regardless of sign.
            charm_abs[k] = charm_abs.get(k, 0.0) + abs(c) * oi * 100 * spot
            vanna_abs[k] = vanna_abs.get(k, 0.0) + abs(v) * oi * 100 * spot

        if not gex_all:
            continue

        def _to_df(d):
            if not d:
                return pd.DataFrame(columns=['strike', 'net_gex'])
            df = pd.DataFrame(list(d.items()), columns=['strike', 'net_gex'])
            return df.sort_values('strike').reset_index(drop=True)

        df_all   = _to_df(gex_all)
        df_0dte  = _to_df(gex_0dte)
        df_multi = _to_df(gex_multi)

        total_gex    = float(df_all['net_gex'].sum())
        levels       = find_key_levels(df_all, spot)
        levels_multi = find_key_levels(df_multi, spot) if not df_multi.empty else levels

        def _ser(lvl):
            return {k: (float(v) if v is not None else None) for k, v in lvl.items()}

        multi_dict = _gex_to_dict(df_multi, spot)
        zero_dict  = _gex_to_dict(df_0dte,  spot)

        # ── TRUE PIN — charm + vanna weighted composite ───────────────────
        # Normalize each greek to [0,1] by dividing by its total across
        # all strikes, then combine with weights: gamma 40%, charm 35%,
        # vanna 25%.  Charm has a natural 1/T amplification, so its
        # contribution to TRUE PIN grows automatically on 0DTE afternoons
        # without any explicit time weighting.
        pin_enhanced = None
        gamma_total = sum(abs(v) for v in gex_all.values()) or 1.0
        charm_total = sum(charm_abs.values()) or 1.0
        vanna_total = sum(vanna_abs.values()) or 1.0

        if gex_all:
            best_score = -1.0
            for k in gex_all:
                g_norm = abs(gex_all.get(k, 0.0)) / gamma_total
                c_norm = charm_abs.get(k, 0.0)    / charm_total
                v_norm = vanna_abs.get(k, 0.0)    / vanna_total
                score  = 0.40 * g_norm + 0.35 * c_norm + 0.25 * v_norm
                if score > best_score:
                    best_score   = score
                    pin_enhanced = k

        # Inject into levels_multi so the frontend can read it alongside pin
        levels_multi_ser = _ser(levels_multi)
        levels_multi_ser['pin_enhanced'] = float(pin_enhanced) if pin_enhanced is not None else None

        data = {
            'symbol':       'SPX',
            'spot':         spot,
            'total_gex':    total_gex,
            'regime':       'POSITIVE' if total_gex > 0 else 'NEGATIVE',
            'levels_multi': levels_multi_ser,
            'levels_0dte':  _ser(find_key_levels(df_0dte, spot)) if not df_0dte.empty else {},
            'multi':        multi_dict,
            'zero':         zero_dict,
            'has_0dte':     not df_0dte.empty,
            'updated':      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'live':         True,   # flag so dashboard knows this is BS-derived
        }

        with _cache_lock:
            existing = _cache.get('$SPX', {})
            # Preserve odte_date from the chain refresh
            data['odte_date'] = existing.get('odte_date')
            _cache['$SPX'] = data

        sse.push({'type': 'gex', 'symbol': 'SPX', **data})
        last_push = time.time()


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
