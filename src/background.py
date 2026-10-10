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
import json
import os
import time
import threading
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from gex import (get_access_token, fetch_option_chain, parse_gex, find_key_levels, get_watch_contracts,
                 TRUE_PIN_WEIGHTS, pick_with_hysteresis, confirmed_pick, sticky_levels, drop_expired)
from price_history import fetch_candles, append_candles, append_new, replace_candles, CACHE_DAYS
import bs
import delta_flow
import flow_alerts
import rolling_profile
import sse
import tos_rtd

_SRC_DIR     = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SRC_DIR)
_DATA_DIR    = os.path.join(_PROJECT_DIR, 'data')

_GEX_SNAPSHOT_FIELDS  = ['timestamp', 'spot', 'total_gex', 'regime',
                          'flip_level', 'put_wall', 'call_wall', 'pin',
                          'strike_min', 'strike_max', 'true_pin', 'charm_flow']
_last_true_pin: dict = {}   # display symbol -> latest TRUE PIN from the live loop
_true_pin_pending: dict = {}  # challenger strike waiting out TRUE_PIN_CONFIRM_SEC
_last_charm_flow: dict = {} # display symbol -> latest charm hedge flow, $/hr
_level_state: dict = {}     # (display symbol, 'multi'|'0dte') -> last published levels (sticky)
_level_seeded: set = set()


def _seed_level_state(sym: str):
    """After a restart, continue from the last saved levels instead of starting
    fresh (a fresh start re-picks between near-equal strikes and the trail jumps)."""
    if sym in _level_seeded:
        return
    _level_seeded.add(sym)
    day = datetime.now().strftime('%Y-%m-%d')
    for tag, kind in (('snapshots', 'multi'), ('0dte_snapshots', '0dte')):
        path = os.path.join(_DATA_DIR, f'gex_{tag}_{sym}_{day}.csv')
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline='') as f:
                rows = list(csv.DictReader(f))
        except OSError:
            continue
        if not rows:
            continue
        last = rows[-1]
        num = lambda v: float(v) if v not in (None, '') else None
        _level_state[(sym, kind)] = {k: num(last.get(k)) for k in ('put_wall', 'call_wall', 'pin')}
        if kind == 'multi' and num(last.get('true_pin')) is not None:
            _last_true_pin.setdefault(sym, num(last.get('true_pin')))
_WATCHLIST_FIELDS     = ['timestamp', 'symbol', 'strike', 'side',
                          'expiry_label', 'delta', 'oi']


def _append_gex_snapshot(sym: str, tag: str, row: dict):
    """Append one row to gex_{tag}_{sym}_{date}.csv, writing header if new."""
    date  = datetime.now().strftime('%Y-%m-%d')
    path  = os.path.join(_DATA_DIR, f'gex_{tag}_{sym}_{date}.csv')
    fields = _GEX_SNAPSHOT_FIELDS
    new_file = not os.path.exists(path)
    if not new_file:
        _upgrade_snapshot_header(path, fields)
    with open(path, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


# ── Strike × expiration GEX history ──────────────────────────────────────────
# One JSONL file per ET day, one line per snapshot:
#   {"ts", "spot", "exp": [expiries], "rows": [[strike, exp_idx, call_oi, put_oi, net_gex], ...]}
# Cells with no OI on either side are left out. The first line of each day
# records the opening OI (it only changes overnight).
_ET               = ZoneInfo('America/New_York')
GRID_INTERVAL_SEC = 50       # snapshot every chain refresh (~60 s); was 300 before 2026-10-07
GRID_KEEP_DAYS    = None     # keep forever (finished days are gzipped by gex_stats.archive_old_files)
_grid_last_ts: dict = {}     # sym -> time.time() of last snapshot
_grid_cleaned_day: dict = {} # sym -> ET date of last cleanup


def _record_gex_grid(sym: str, raw_df, spot: float):
    now = time.time()
    if now - _grid_last_ts.get(sym, 0) < GRID_INTERVAL_SEC:
        return
    et = datetime.now(_ET)
    if et.weekday() >= 5 or not (dtime(9, 30) <= et.time() < dtime(16, 0)):
        return
    if raw_df is None or raw_df.empty:
        return
    _grid_last_ts[sym] = now

    df   = raw_df.assign(call_oi=raw_df['oi'].where(raw_df['type'] == 'call', 0),
                         put_oi=raw_df['oi'].where(raw_df['type'] == 'put', 0))
    cell = (df.groupby(['expiration', 'strike'])[['call_oi', 'put_oi', 'gex']]
              .sum().reset_index())
    cell = cell[(cell['call_oi'] > 0) | (cell['put_oi'] > 0)]
    exps = sorted(cell['expiration'].unique())
    idx  = {e: i for i, e in enumerate(exps)}
    rows = [[float(r.strike), idx[r.expiration], int(r.call_oi), int(r.put_oi), round(float(r.gex))]
            for r in cell.itertuples(index=False)]

    day  = et.date().isoformat()
    path = os.path.join(_DATA_DIR, f'gex_grid_{sym}_{day}.jsonl')
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': et.isoformat(timespec='seconds'), 'spot': float(spot),
                                'exp': exps, 'rows': rows}, separators=(',', ':')) + '\n')
    except OSError as e:
        print(f'[GEX grid] {sym}: write failed: {e}')

    if GRID_KEEP_DAYS is not None and _grid_cleaned_day.get(sym) != day:
        _grid_cleaned_day[sym] = day
        prefix = f'gex_grid_{sym}_'
        for name in os.listdir(_DATA_DIR):
            if not (name.startswith(prefix) and name.endswith('.jsonl')):
                continue
            try:
                d = datetime.strptime(name[len(prefix):-len('.jsonl')], '%Y-%m-%d').date()
                if (et.date() - d).days > GRID_KEEP_DAYS:
                    os.remove(os.path.join(_DATA_DIR, name))
            except (ValueError, OSError):
                continue


def _upgrade_snapshot_header(path: str, fields: list):
    """Add columns introduced after the file was started (e.g. true_pin) so
    appended rows line up with the header. Existing rows get blanks."""
    with open(path, newline='') as f:
        header = next(csv.reader(f), [])
    if header == fields or not header:
        return
    with open(path, newline='') as f:
        rows = list(csv.DictReader(f))
    tmp = path + '.tmp'
    with open(tmp, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def load_level_history(sym: str, day=None) -> list:
    """A day's GEX level history (default today) for the price trail charts.

    Rows come from gex_snapshots_<sym>_<date>.csv, written once per chain refresh
    during market hours. Timestamps there are naive local time; returned 'time'
    is epoch seconds to match the candle data.
    """
    date = day.isoformat() if day else datetime.now().strftime('%Y-%m-%d')
    path = os.path.join(_DATA_DIR, f'gex_snapshots_{sym}_{date}.csv')
    if not os.path.exists(path):
        return []

    def num(v):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    out = []
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            try:
                t = int(datetime.strptime(r['timestamp'], '%Y-%m-%d %H:%M:%S').timestamp())
            except (KeyError, ValueError):
                continue
            out.append({'time': t, 'spot': num(r.get('spot')),
                        'flip_level': num(r.get('flip_level')), 'put_wall': num(r.get('put_wall')),
                        'call_wall': num(r.get('call_wall')), 'pin': num(r.get('pin')),
                        'pin_enhanced': num(r.get('true_pin'))})
    return out


def daily_jobs_loop():
    """Once a day after the close: write the summary row, then gzip finished days.
    On startup it also backfills summaries for past days and archives old files."""
    import gex_stats
    def run(write_today: bool):
        if write_today:
            # since the previous run (Friday's on a Monday), before the summary reads the bars
            hours = 72 if datetime.now(_ET).weekday() == 0 else 24
            for sym in _price_symbols:
                try:
                    reconcile_recent(sym, hours)
                except Exception as e:
                    print(f'[daily] {sym}: reconcile failed: {e}')
        try:
            for d in gex_stats.summary_days_missing('SPX'):
                gex_stats.write_daily_summary(d, 'SPX')
            for d in gex_stats.scorecard_days_missing('SPX'):
                gex_stats.write_scorecard(d, 'SPX')
            if write_today:
                gex_stats.write_daily_summary(datetime.now(_ET).date(), 'SPX')
                gex_stats.write_scorecard(datetime.now(_ET).date(), 'SPX')
            done = gex_stats.archive_old_files('SPX')
            if done:
                print(f'[daily] archived {len(done)} file(s)')
        except Exception as e:  # never kill the thread
            print(f'[daily] job failed: {e}')

    run(write_today=False)
    written_for = None
    while True:
        time.sleep(600)
        now = datetime.now(_ET)
        if now.weekday() < 5 and now.time() >= dtime(16, 20) and written_for != now.date():
            run(write_today=True)
            written_for = now.date()


# ── Futures buy/sell volume (tick rule, from the streamer) ────────────────────
_vol_split: dict = {}        # display sym -> {minute_ms: (buy, sell)} for the live session
_vol_split_last: dict = {}   # display sym -> latest minute seen (to persist on rollover)
_vol_split_lock = threading.Lock()


def _vol_split_path(sym: str, minute_ms: int) -> str:
    day = datetime.fromtimestamp(minute_ms / 1000, _ET).date().isoformat()
    return os.path.join(_DATA_DIR, f'volume_split_{sym}_{day}.csv')


def on_volume_split(symbol: str, minute_ms: int, buy: float, sell: float):
    """Streamer callback: running buy/sell volume for the current minute."""
    sym = symbol.replace('/', '').replace('$', '')
    with _vol_split_lock:
        prev = _vol_split_last.get(sym)
        book = _vol_split.setdefault(sym, {})
        if prev is not None and minute_ms > prev and prev in book:
            b, s = book[prev]
            path = _vol_split_path(sym, prev)
            new = not os.path.exists(path)
            try:
                with open(path, 'a', newline='') as f:
                    if new:
                        f.write('time,buy,sell\n')
                    f.write(f'{prev // 1000},{round(b)},{round(s)}\n')
            except OSError as e:
                print(f'[volume split] write failed: {e}')
        book[minute_ms] = (buy, sell)
        _vol_split_last[sym] = max(prev or 0, minute_ms)
        for k in [k for k in book if k < minute_ms - 6 * 3_600_000]:
            del book[k]


def load_volume_split(sym: str = 'ES', days: int = 2, day=None) -> dict:
    """{epoch_sec: [buy, sell]} from the saved day files plus the live minute.
    With `day`, only that day's file (for replay)."""
    import glob as _glob
    out = {}
    paths = ([os.path.join(_DATA_DIR, f'volume_split_{sym}_{day.isoformat()}.csv')] if day else
             sorted(_glob.glob(os.path.join(_DATA_DIR, f'volume_split_{sym}_*.csv')))[-days:])
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline='') as f:
                for r in csv.DictReader(f):
                    out[int(r['time'])] = [float(r['buy']), float(r['sell'])]
        except (OSError, ValueError, KeyError):
            continue
    if day is None:
        with _vol_split_lock:
            for m, (b, s) in _vol_split.get(sym, {}).items():
                out[m // 1000] = [b, s]
    return out


def _last_session_path(sym: str) -> str:
    return os.path.join(_DATA_DIR, f'gex_last_session_{sym}.json')


def _save_last_session(sym: str, data: dict):
    """Keep the latest in-session GEX/DDOI result so it can be shown after the close."""
    path = _last_session_path(sym)
    tmp  = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, default=float)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        print(f'[GEX] {sym}: could not save last session: {e}')


def _local_to_et(stamp):
    """'YYYY-MM-DD HH:MM:SS' in this PC's local time (Pacific) -> the same in ET, as the
    dashboard shows ET everywhere ('last session 15:09' was really 18:09 ET)."""
    try:
        return (datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').astimezone(_ET)
                .strftime('%Y-%m-%d %H:%M:%S'))
    except (TypeError, ValueError):
        return stamp


def _load_last_session(sym: str):
    try:
        with open(_last_session_path(sym), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


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
        raw_chain    = fetch_option_chain(symbol, token, strike_count=strike_count)
        chain        = drop_expired(raw_chain)   # after 16:00 ET the day's expiry is gone
        gex_all, gex_0dte, gex_multi, spot, raw_df = parse_gex(chain)
        display_sym  = symbol.replace('$', '').replace('/', '')
        _seed_level_state(display_sym)
        levels       = find_key_levels(gex_all, spot)
        total_gex    = float(gex_all['net_gex'].sum())

        levels_multi = find_key_levels(gex_multi, spot) if not gex_multi.empty else levels
        levels_0dte  = find_key_levels(gex_0dte,  spot) if not gex_0dte.empty  else {}
        # sticky: near-equal strikes don't swap every refresh
        levels_multi = sticky_levels(levels_multi, _level_state.get((display_sym, 'multi')), gex_multi, spot)
        _level_state[(display_sym, 'multi')] = dict(levels_multi)
        if levels_0dte:
            levels_0dte = sticky_levels(levels_0dte, _level_state.get((display_sym, '0dte')), gex_0dte, spot)
            _level_state[(display_sym, '0dte')] = dict(levels_0dte)

        def serialize_levels(lvl):
            return {k: (float(v) if v is not None else None) for k, v in lvl.items()}

        odte_date = None
        for exp_key in chain.get('callExpDateMap', {}).keys():
            if exp_key.endswith(':0'):
                odte_date = exp_key.split(':')[0]
                break

        multi_dict = _gex_to_dict(gex_multi, spot)
        zero_dict  = _gex_to_dict(gex_0dte,  spot)

        # ── DDOI: per-strike call/put OI across all expiries ─────────────────
        # Assumes dealers are net short options (standard for SPX market makers).
        # Call DDOI = call OI per strike → dealer short calls → buy pressure above spot
        # Put  DDOI = put  OI per strike → dealer short puts  → sell pressure below spot
        ddoi_calls = {}   # strike -> total call OI
        ddoi_puts  = {}   # strike -> total put OI
        lo_ddoi = spot * 0.94
        hi_ddoi = spot * 1.06
        for exp_key, strikes in chain.get('callExpDateMap', {}).items():
            for strike_str, opts in strikes.items():
                strike = float(strike_str)
                if not (lo_ddoi <= strike <= hi_ddoi):
                    continue
                oi = int(opts[0].get('openInterest') or 0) if opts else 0
                ddoi_calls[strike] = ddoi_calls.get(strike, 0) + oi
        for exp_key, strikes in chain.get('putExpDateMap', {}).items():
            for strike_str, opts in strikes.items():
                strike = float(strike_str)
                if not (lo_ddoi <= strike <= hi_ddoi):
                    continue
                oi = int(opts[0].get('openInterest') or 0) if opts else 0
                ddoi_puts[strike] = ddoi_puts.get(strike, 0) + oi
        all_ddoi_strikes = sorted(set(ddoi_calls) | set(ddoi_puts))
        ddoi_dict = {
            'strikes':  all_ddoi_strikes,
            'call_oi':  [ddoi_calls.get(k, 0) for k in all_ddoi_strikes],
            'put_oi':   [ddoi_puts.get(k, 0)  for k in all_ddoi_strikes],
        }

        data = {
            'symbol':       symbol.replace('$', '').replace('/', ''),
            'spot':         spot,
            'total_gex':    total_gex,
            'regime':       'POSITIVE' if total_gex > 0 else 'NEGATIVE',
            # keep the live loop's TRUE PIN through the chain refresh (it isn't recomputed here)
            'levels_multi': {**serialize_levels(levels_multi),
                             'pin_enhanced': _last_true_pin.get(symbol.replace('$', '').replace('/', ''))},
            'levels_0dte':  serialize_levels(levels_0dte),
            'multi':        multi_dict,
            'zero':         zero_dict,
            'has_0dte':     not gex_0dte.empty,
            'odte_date':    odte_date,
            'ddoi':         ddoi_dict,
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
            'underlying':  c.get('underlying') or display_sym,
            'gamma':       c.get('gamma', 0.0),
        } for c in watch}
        _oi_cache.update(new_oi)
        _contract_meta.update(new_meta)

        # ── Subscribe contracts to TOS RTD (always subscribe when registered,
        #    so quotes are ready the moment the user toggles RTD on) ──────────
        if tos_rtd.is_available():
            tos_syms = [tos_rtd.contract_to_tos(c) for c in watch
                        if c.get('expiry_date')]
            tos_rtd.add_symbols(tos_syms)
            # Store TOS symbol on meta so live_gex_loop can look it up
            for c in watch:
                if c.get('expiry_date') and c['symbol'] in _contract_meta:
                    _contract_meta[c['symbol']]['tos_symbol'] = tos_rtd.contract_to_tos(c)

        # ── Detect post-market CLOSED state (OI zeroed after expiry) ────────
        # Schwab zeroes SPX open interest after the close. OI only changes
        # overnight, so fall back to the last in-session result instead.
        all_oi_zero = all(c['oi'] == 0 for c in watch) and             not any(ddoi_dict['call_oi']) and not any(ddoi_dict['put_oi'])
        if all_oi_zero:
            last = _load_last_session(display_sym)
            if last:
                data = {**last, 'spot': spot, 'regime': 'CLOSED',
                        'last_session': _local_to_et(last.get('updated'))}
            else:
                data['regime'] = 'CLOSED'
        else:
            _save_last_session(display_sym, data)

        with _cache_lock:
            _cache[symbol] = data

        # ── Persist snapshots ────────────────────────────────────────────────
        # Skipped after the close: zero-OI refreshes would log GEX as 0, and
        # until then the rows only repeat tomorrow's book (OI changes overnight).
        after_close = datetime.now(_ET).time() >= dtime(16, 0)
        if not all_oi_zero and not after_close:
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
                'true_pin':   _last_true_pin.get(display_sym, ''),
                'charm_flow': round(_last_charm_flow[display_sym]) if display_sym in _last_charm_flow else '',
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
                    'true_pin':   '',
                })

            _append_watchlist(display_sym, ts, watch)
            _record_gex_grid(display_sym, raw_df, spot)
        # ─────────────────────────────────────────────────────────────────────

        if display_sym == 'SPX':
            new_snap = delta_flow.extract_chain_snapshot(raw_chain)
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

def recent_candles(candles: list) -> list:
    """The last CACHE_DAYS of candles: what the live chart keeps in memory."""
    cutoff = (time.time() - CACHE_DAYS * 86400) * 1000
    return [c for c in candles if c['datetime'] >= cutoff]


def _merge_candles(cached: list, rows: list) -> list:
    """Cached candles with `rows` replacing or adding their minutes, sorted, recent only."""
    by_dt = {c['datetime']: c for c in cached}
    by_dt.update({c['datetime']: c for c in rows})
    return recent_candles([by_dt[k] for k in sorted(by_dt)])


def reconcile_recent(symbol: str, hours: float) -> int:
    """Replace the last `hours` of saved 1-min bars with Schwab's history. A streamed
    final bar can miss trades reported late (23 of 975 /ES minutes differed on
    2026-10-08); REST history has them. Runs once after the close. Only replaces or
    adds minutes, never removes any."""
    token = get_access_token()
    now_ms = int(time.time() * 1000)
    rest = fetch_candles(symbol, token, frequency=1,
                         start_ms=now_ms - int(hours * 3600_000), end_ms=now_ms)
    if not rest:
        return 0
    with _csv_lock:
        changed = replace_candles(symbol, rest)
    if changed:
        with _cache_lock:
            _candle_cache[symbol] = _merge_candles(_candle_cache.get(symbol, []),
                                                   [c for c in rest if c['datetime'] + 60_000 <= now_ms])
    print(f'[daily] {symbol}: {changed} saved minute(s) corrected from Schwab history')
    return changed


def refresh_price(symbol: str):
    try:
        token       = get_access_token()
        total_added = 0

        new_rows = []
        historical = fetch_candles(symbol, token, days=2, frequency=1)
        if historical:
            with _csv_lock:
                new_rows += append_new(symbol, historical)

        today_midnight = datetime.now(tz=ET).replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(today_midnight.timestamp() * 1000)
        end_ms   = int((time.time() + 3600) * 1000)
        live = fetch_candles(symbol, token, frequency=1, start_ms=start_ms, end_ms=end_ms)
        if live:
            with _csv_lock:
                added = append_new(symbol, live)
            new_rows += added
            if added:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Price live: {symbol} +{len(added)} candles today")
        total_added = len(new_rows)

        if new_rows:
            with _cache_lock:
                # Newly saved bars replace whatever the cache had for those minutes (a
                # partial tick-built candle); streamed candles newer than them stay.
                _candle_cache[symbol] = _merge_candles(_candle_cache.get(symbol, []), new_rows)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Price updated: {symbol} +{total_added} total")

            # Push the latest candle to the browser so it updates without a reload.
            # Map raw CSV symbol → browser chart key (same as on_streamer_candle).
            _SYM_MAP = {'/ES': 'ES', '$SPX': 'SPX'}
            browser_sym = _SYM_MAP.get(symbol, symbol)
            if new_rows:
                last = max(new_rows, key=lambda c: c['datetime'])
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

    # Streamed cumulative volume feeds the rolling 0DTE profile in near real time
    meta = _contract_meta.get(sym)
    if meta and meta.get('is_0dte') and quote.get('volume') is not None:
        rolling_profile.record_stream_volume(meta.get('underlying') or 'SPX', sym,
                                             meta['strike'], meta['side'], quote['volume'])


def _sticky_0dte(levels: dict, df, spot: float) -> dict:
    """Live 0DTE levels share stickiness with the chain refresh's 0DTE levels."""
    out = sticky_levels(levels, _level_state.get(('SPX', '0dte')), df, spot)
    _level_state[('SPX', '0dte')] = dict(out)
    return out


def live_gex_loop():
    """
    Recomputes GEX from live streamer bid/ask every ~2 s when quotes change.
    Replaces chain-gamma with BS gamma derived from the current bid/ask mid.
    """
    import pandas as pd
    from datetime import date

    MIN_INTERVAL = 2.0
    last_push    = 0.0
    last_log     = 0.0

    while True:
        _gex_dirty.wait(timeout=MIN_INTERVAL)
        _gex_dirty.clear()

        now = time.time()
        if now - last_push < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - (now - last_push))

        # Spot: the streamed SPX price (ticks ~1 s) while it's current, else the chain's
        # underlying from the 60 s refresh. With only the chain's, spot was up to 60 s
        # old: IVs were solved from live option quotes against a stale price, and each
        # push dragged the dashboard's spot line and price back (seen 2026-10-09).
        spot = 0.0
        with _cache_lock:
            spx_data = _cache.get('$SPX') or _cache.get('SPX')
            if spx_data:
                spot = spx_data.get('spot', 0.0)
            bars = _candle_cache.get('$SPX') if _candle_cache is not None else None
            if bars and time.time() * 1000 - bars[-1]['datetime'] < 120_000:
                spot = float(bars[-1].get('close') or 0.0) or spot
        if spot <= 0.0:
            continue

        today = date.today()

        # Snapshot caches (avoid holding lock during BS computation)
        meta_snap  = dict(_contract_meta)
        oi_snap    = dict(_oi_cache)
        quote_snap = dict(_quote_cache)

        if not meta_snap or not quote_snap:
            continue

        # ── Quotes per (expiry, strike) ──────────────────────────────────
        use_rtd = tos_rtd.is_enabled() and tos_rtd._connected
        rtd_hits = 0
        schwab_hits = 0
        cells = {}       # (expiry, strike) -> {'call': (sym, meta, bid, ask), 'put': ...}
        for sym, meta in meta_snap.items():
            # Prefer TOS RTD bid/ask (lower latency) over Schwab streamer cache
            if use_rtd and meta.get('tos_symbol'):
                bid, ask = tos_rtd.get_quote(meta['tos_symbol'])
                # Fall back to Schwab cache if RTD hasn't received a quote yet
                if ask <= 0.0:
                    bid, ask = quote_snap.get(sym, (0.0, 0.0))
                    schwab_hits += 1
                else:
                    rtd_hits += 1
            else:
                bid, ask = quote_snap.get(sym, (0.0, 0.0))
                schwab_hits += 1
            if not meta.get('expiry_date'):
                continue
            cells.setdefault((meta['expiry_date'], meta['strike']), {})[meta['side']] = (sym, meta, bid, ask)

        # ── Per-contract GEX, charm, vanna ───────────────────────────────
        # One vol per strike, solved from the OTM side (calls above spot, puts
        # below): OTM quotes are liquid and all time value, ITM ones are mostly
        # intrinsic and often fail to invert. Calls and puts at a strike share
        # gamma, so the same vol serves both. If neither side inverts, fall back
        # to Schwab's chain gamma (charm/vanna 0) rather than dropping the strike.
        gex_0dte  = {}   # strike -> net_gex (signed: calls+, puts-)
        gex_watch = {}   # all watched contracts (0DTE + nearest expiry) — TRUE PIN input
        charm_abs = {}   # strike -> sum(|charm| * oi * 100 * spot)
        vanna_abs = {}   # strike -> sum(|vanna| * oi * 100 * spot)
        charm_flow = 0.0  # signed dealer charm exposure, $ of delta per year (+ = dealers buy)
        n_solved = n_fallback = 0
        T_by_exp = {}

        for (expiry_str, k), sides in cells.items():
            if expiry_str not in T_by_exp:
                try:
                    T_by_exp[expiry_str] = bs.t_to_close(expiry_str)
                except Exception:
                    T_by_exp[expiry_str] = 0.0
            T = T_by_exp[expiry_str]
            if T <= 0.0:
                continue

            sigma = None
            otm, itm = ('call', 'put') if k >= spot else ('put', 'call')
            for side in (otm, itm):
                q = sides.get(side)
                if not q or q[3] <= 0.0:
                    continue
                bid, ask = q[2], q[3]
                mid = (bid + ask) * 0.5 if bid > 0.0 else ask * 0.5
                sigma = bs.iv(mid, spot, k, T, _RISK_FREE, side[0])
                if sigma is not None:
                    break

            if sigma is not None:
                g, c, v = bs.greeks(spot, k, T, _RISK_FREE, sigma)
                n_solved += 1
            else:
                g = c = v = None
                n_fallback += 1

            for side, (sym, meta, _, _) in sides.items():
                oi = oi_snap.get(sym, 0)
                if oi == 0:
                    continue
                gamma = g if g is not None else float(meta.get('gamma') or 0.0)
                sign  = 1 if side == 'call' else -1
                gex   = gamma * oi * 100 * spot * sign
                gex_watch[k] = gex_watch.get(k, 0.0) + gex
                if meta.get('is_0dte'):
                    gex_0dte[k] = gex_0dte.get(k, 0.0) + gex
                # Charm and vanna: absolute value — both calls and puts drive
                # rehedging flows toward the pinned strike regardless of sign.
                charm_abs[k] = charm_abs.get(k, 0.0) + abs(c or 0.0) * oi * 100 * spot
                # Dealers long calls / short puts: as time passes their option delta
                # moves by -charm, so the hedge flow is +sign*charm (buy if > 0).
                charm_flow  += (c or 0.0) * oi * 100 * spot * sign
                vanna_abs[k] = vanna_abs.get(k, 0.0) + abs(v or 0.0) * oi * 100 * spot

        if not gex_0dte:
            continue

        def _to_df(d):
            if not d:
                return pd.DataFrame(columns=['strike', 'net_gex'])
            df = pd.DataFrame(list(d.items()), columns=['strike', 'net_gex'])
            return df.sort_values('strike').reset_index(drop=True)

        df_0dte = _to_df(gex_0dte)

        def _ser(lvl):
            return {k: (float(v) if v is not None else None) for k, v in lvl.items()}

        zero_dict = _gex_to_dict(df_0dte, spot)

        # ── TRUE PIN — charm + vanna weighted composite ───────────────────
        # Normalize each greek to [0,1] by dividing by its total across
        # all strikes, then combine with TRUE_PIN_WEIGHTS (gex.py; backtested,
        # see tools/pin_backtest.py).
        gamma_total = sum(abs(v) for v in gex_watch.values()) or 1.0
        charm_total = sum(charm_abs.values()) or 1.0
        vanna_total = sum(vanna_abs.values()) or 1.0
        scores = {
            k: (TRUE_PIN_WEIGHTS['gamma'] * abs(gex_watch.get(k, 0.0)) / gamma_total
                + TRUE_PIN_WEIGHTS['charm'] * charm_abs.get(k, 0.0) / charm_total
                + TRUE_PIN_WEIGHTS['vanna'] * vanna_abs.get(k, 0.0) / vanna_total)
            for k in gex_watch
        }
        # sticky: only move when a new strike clearly beats the current one, and has
        # done so for TRUE_PIN_CONFIRM_SEC (an unwatched current strike switches at once)
        cur_pin = _last_true_pin.get('SPX')
        cur_pin = cur_pin if cur_pin in scores else None
        pin_enhanced = confirmed_pick(pick_with_hysteresis(scores, cur_pin), cur_pin,
                                      _true_pin_pending, time.time())

        with _cache_lock:
            existing = dict(_cache.get('$SPX', {}))
        # Only 0DTE is recomputed live: the stream covers just today's book plus
        # the top strikes of one other expiry, so ALL EXPIRATIONS keeps the full
        # chain from the 60 s refresh. TRUE PIN is injected into its levels.
        multi_dict       = existing.get('multi') or {'strikes': [], 'net_gex': []}
        levels_multi_ser = dict(existing.get('levels_multi') or {})
        levels_multi_ser['pin_enhanced'] = float(pin_enhanced) if pin_enhanced is not None else None
        if pin_enhanced is not None:
            _last_true_pin['SPX'] = float(pin_enhanced)
        _last_charm_flow['SPX'] = charm_flow / (365.0 * 24)
        total_gex = float(sum(multi_dict.get('net_gex') or [])) + float(df_0dte['net_gex'].sum())

        data = {
            **existing,                     # keeps ddoi, odte_date and other chain fields
            'symbol':       'SPX',
            'spot':         spot,
            'total_gex':    total_gex,
            'regime':       'POSITIVE' if total_gex > 0 else 'NEGATIVE',
            'levels_multi': levels_multi_ser,
            'levels_0dte':  _ser(_sticky_0dte(find_key_levels(df_0dte, spot), df_0dte, spot)),
            'multi':        multi_dict,
            'zero':         zero_dict,
            'has_0dte':     True,
            'updated':      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'live':         True,   # flag so dashboard knows 0DTE is BS-derived
            # Hedge flow from charm alone, $ per hour of clock time. Backtest
            # (tools/pin_backtest.py): its sign matched the move into the close
            # on 71-76% of days at 13:00-15:00 (2026-06 to 10, ~22 days).
            'charm_flow_per_hr': charm_flow / (365.0 * 24),
        }
        data.pop('last_session', None)  # live data is never the after-hours fallback

        with _cache_lock:
            _cache['$SPX'] = data

        sse.push({'type': 'gex', 'symbol': 'SPX', **data})
        last_push = time.time()
        if last_push - last_log >= 60:      # every 2 s was ~13k log lines a day
            last_log = last_push
            src = f'RTD:{rtd_hits} Schwab:{schwab_hits}' if use_rtd else f'Schwab:{schwab_hits}'
            print(f'[GEX live] quote sources — {src}  vol solved {n_solved} / Schwab gamma fallback {n_fallback}')


# ── Streamer callbacks ────────────────────────────────────────────────────────

def in_trading_hours(symbol: str, ts_ms: int) -> bool:
    """Is this minute inside the symbol's session? On subscribe Schwab sends the last
    price even when the market is shut, which made a stray candle for the current
    minute (a Saturday SPX and ES bar on 2026-10-10, so the chart's last two days
    were Friday plus that one candle)."""
    t = datetime.fromtimestamp(ts_ms / 1000, _ET)
    wd, hm = t.weekday(), t.time()
    if symbol == '$SPX':
        return wd < 5 and dtime(9, 30) <= hm < dtime(16, 0)
    if symbol == '/ES':   # CME Globex: Sun 18:00 - Fri 17:00 ET, halted 17:00-18:00 daily
        if wd == 5 or (wd == 6 and hm < dtime(18, 0)) or (wd == 4 and hm >= dtime(17, 0)):
            return False
        return not (dtime(17, 0) <= hm < dtime(18, 0))
    return True


def on_streamer_candle(candle: dict):
    """
    Called by SchwabStreamer on each incoming candle update.
    Updates candle_cache, pushes SSE, persists completed bars to CSV.
    """
    raw_symbol = candle['symbol']
    ts_ms      = candle['datetime']
    if raw_symbol == '$SPX':
        rolling_profile.update_spot('SPX', candle.get('close'))
    if not in_trading_hours(raw_symbol, ts_ms):
        return
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
            elif is_final:
                # Schwab's completed bar arrives a moment after its minute ends, when
                # live ticks have already started the next candle. Replace the partial
                # tick-built candle for that minute instead of dropping the final one.
                for i in range(len(existing) - 1, max(-1, len(existing) - 11), -1):
                    if existing[i]['datetime'] == ts_ms:
                        existing[i] = candle
                        push_sse  = True
                        write_csv = True
                        break
            if ts_ms == last_ts and is_final:
                write_csv = True      # the completed bar for the current last minute: persist it
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

    if write_csv:   # one line per saved bar (every tick was ~46k lines a day)
        ts = datetime.now().strftime('%H:%M:%S')
        print(f'[{ts}] Streamer candle: {raw_symbol} {candle["close"]:.2f} [saved]')


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
