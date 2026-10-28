"""
Red days, and dealer positioning into them.

Red days are expirations that matter more than a regular daily one: monthly / quarterly
opex, month / quarter / year end, and big scheduled news (FOMC, CPI, jobs report). They
come from reference/market_events.csv (tools/build_event_calendar.py).

positioning(day) follows that one expiration as it gets closer: open interest and GEX per
strike at the close of each recorded session (the last snapshot in each
data/gex_full_SPX_<date>.jsonl, recorded since 2026-10-15) and now (today's latest
snapshot, up to 15 min old). Open interest changes once a day, overnight, so the change
from one session to the next is where positions were added or closed; GEX also moves with
spot, vol and time left. GEX is worked out from each contract's implied vol: Schwab's
chain gamma is rounded to 3 decimals, which zeroes most longer-dated contracts.

build_notes() turns that into a dated note per session: what grew and shrank (and whether
the previous session's trades in those strikes were mostly bought or sold), the move the
market prices to the expiration (ATM straddle), the biggest gamma either side of spot and
whether the whole book agrees, the expiration's magnets (max pain, biggest open interest,
gamma flip), and what SPX did the next session against those levels, scored. Finished
sessions' notes are saved to data/key_date_notes_SPX.jsonl for the model.
"""
import csv
import glob
import json
import os
import statistics
import threading
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import bs
from gex_stats import archived, day_file, open_text, week_end
from price_history import load_range

ET = ZoneInfo('America/New_York')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_CSV = os.path.join(ROOT, 'reference', 'market_events.csv')
DATA_DIR = os.path.join(ROOT, 'data')
RISK_FREE = 0.05            # background._RISK_FREE
SNAP_CACHE_MAX = 30         # parsed full-chain snapshots kept (one per session file)
NOTES_FILE = os.path.join(DATA_DIR, 'key_date_notes_SPX.jsonl')
TAG_CATEGORY = {'FOMC': 'fomc', 'CPI': 'cpi', 'JOBS REPORT': 'jobs', 'MONTHLY OPEX': 'opex',
                'QUARTERLY OPEX': 'opex', 'MONTH END': 'period_end', 'QUARTER END': 'period_end',
                'YEAR END': 'period_end'}

_lock = threading.Lock()
_notes_lock = threading.Lock()  # one request at a time reads, merges and replaces the notes file
_events = {'sig': None, 'red': {}, 'holidays': set()}
_snaps: dict = {}           # file path -> ((size, mtime), table)
_sides: dict = {}           # chain file -> ((size, mtime), {(expiry, strike, 'c'|'p'): [bought, sold, between]})
_ohlc: dict = {}            # finished session -> SPX open / high / low / close
_notes_saved = {'sig': None, 'rows': []}


def _tag(row: dict) -> str | None:
    cat, ev = row.get('category', ''), row.get('event', '').lower()
    if cat == 'opex':
        return 'QUARTERLY OPEX' if 'quarterly' in ev else 'MONTHLY OPEX'
    if cat == 'period_end':
        return 'YEAR END' if 'year' in ev else 'QUARTER END' if 'quarter' in ev else 'MONTH END'
    return {'fomc': 'FOMC', 'cpi': 'CPI', 'jobs': 'JOBS REPORT'}.get(cat)


def _load_events(path: str = EVENTS_CSV) -> dict:
    st = os.stat(path)
    sig = (path, st.st_size, st.st_mtime)
    with _lock:
        if _events['sig'] == sig:
            return _events
    red, holidays = {}, set()
    with open(path, encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            if row.get('category') == 'holiday':
                holidays.add(row['date'])
            t = _tag(row)
            if t and t not in red.get(row['date'], []):
                red.setdefault(row['date'], []).append(t)
    with _lock:
        _events.update(sig=sig, red=red, holidays=holidays)
    return _events


def red_days(path: str = EVENTS_CSV) -> dict:
    """{date: [tags]} for every red day in the calendar."""
    return _load_events(path)['red']


def sessions_away(today: str, day: str, path: str = EVENTS_CSV) -> int:
    """Trading sessions after `today` up to and including `day` (0 = today)."""
    holidays = _load_events(path)['holidays']
    d, end, n = date.fromisoformat(today), date.fromisoformat(day), 0
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in holidays:
            n += 1
    return n


def next_red_days(n: int = 2, now: datetime | None = None, path: str = EVENTS_CSV) -> list:
    """The next n red days: [(date, tags)]. Today counts until its 16:00 close."""
    now = now or datetime.now(ET)
    today = now.date().isoformat()
    red = red_days(path)
    open_today = now.time() < dtime(16, 0)
    days = sorted(d for d in red if d > today or (d == today and open_today))
    return [(d, red[d]) for d in days[:n]]


# ── positioning ────────────────────────────────────────────────────────────────

def _last_snapshot(path: str) -> dict | None:
    """The file's last complete snapshot (a line still being written doesn't parse)."""
    with open_text(path) as f:
        lines = [line for line in f if line.strip()]
    for line in reversed(lines[-2:]):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _table(snap: dict) -> dict:
    """One snapshot: {'ts', 'spot', 'exp': {expiry: {strike: [call_oi, put_oi, gex]}},
    'atm': {expiry: {strike, straddle, iv}}, 'walls_30d', 'walls_week'}.
    GEX = gamma x OI x 100 x spot, calls +, puts -, gamma from the contract's vol. The walls
    are the 3 biggest |GEX| strikes within 150 pts of spot over expirations in the next 30
    days (the heatmap's All) and through that week's Friday (Week)."""
    when = datetime.fromisoformat(snap['ts'])
    spot = float(snap['spot'])
    f = {name: i for i, name in enumerate(snap['fields'])}
    t_by_exp, out, near = {}, {}, {}
    for r in snap['rows']:
        exp, k = snap['exp'][r[f['exp_idx']]], float(r[f['strike']])
        call, iv = r[f['side']] == 0, r[f['iv']]
        if 'mark' in f and abs(k - spot) <= 30:              # for the ATM straddle
            slot = near.setdefault(exp, {}).setdefault(k, [None, None, None, None])
            slot[0 if call else 1], slot[2 if call else 3] = r[f['mark']], iv
        oi = int(r[f['oi']] or 0)
        if oi <= 0:
            continue
        if exp not in t_by_exp:
            t_by_exp[exp] = bs.t_to_close(exp, now=when)
        T = t_by_exp[exp]
        g = bs._bs_gamma(spot, k, T, RISK_FREE, float(iv) / 100) if iv and 0 < float(iv) < 500 and T > 0 else 0.0
        cell = out.setdefault(exp, {}).setdefault(k, [0, 0, 0.0])
        cell[0 if call else 1] += oi
        cell[2] += (g if call else -g) * oi * 100 * spot
    atm = {}
    for exp, by_k in near.items():
        both = sorted((abs(k - spot), k) for k, v in by_k.items() if v[0] and v[1])
        if both:
            k = both[0][1]
            v = by_k[k]
            ivs = [float(x) for x in v[2:] if x and 0 < float(x) < 500]
            atm[exp] = {'strike': k, 'straddle': round(float(v[0]) + float(v[1]), 2),
                        'iv': round(sum(ivs) / len(ivs), 1) if ivs else None}
    day = when.astimezone(ET).date()
    in30, fri = (day + timedelta(days=30)).isoformat(), week_end(day.isoformat())
    a30, wk = {}, {}
    for exp, cells in out.items():
        for k, c in cells.items():
            if abs(k - spot) <= 150:
                if exp <= in30:
                    a30[k] = a30.get(k, 0.0) + c[2]
                if exp <= fri:
                    wk[k] = wk.get(k, 0.0) + c[2]
    top3 = lambda a: sorted(a, key=lambda k: -abs(a[k]))[:3]
    return {'ts': snap['ts'], 'spot': spot, 'exp': out, 'atm': atm,
            'walls_30d': top3(a30), 'walls_week': top3(wk)}


def expiry_stats(cells: dict, spot: float) -> dict:
    """An expiration's magnets: max pain (the strike where its options are worth least at
    expiry), its gamma flip (where cumulative GEX from the lowest strike turns positive)
    and its 3 biggest open-interest strikes within 10% of spot."""
    ks = sorted(cells)
    near = [k for k in ks if abs(k - spot) <= spot * 0.10]
    if not ks:
        return {'max_pain': None, 'flip': None, 'top_oi': []}
    pain = lambda x: sum(max(0.0, x - k) * c[0] + max(0.0, k - x) * c[1] for k, c in cells.items())
    cum, flip = 0.0, None
    for k in ks:
        prev, cum = cum, cum + cells[k][2]
        if flip is None and prev < 0 <= cum:
            flip = k
    return {'max_pain': min(near or ks, key=pain), 'flip': flip,
            'top_oi': sorted(near, key=lambda k: -(cells[k][0] + cells[k][1]))[:3]}


def _file_table(path: str) -> dict | None:
    path = archived(path)
    st = os.stat(path)
    sig = (st.st_size, st.st_mtime)
    with _lock:
        hit = _snaps.get(path)
        if hit and hit[0] == sig:
            return hit[1]
    snap = _last_snapshot(path)
    table = _table(snap) if snap else None
    with _lock:
        _snaps[path] = (sig, table)
        while len(_snaps) > SNAP_CACHE_MAX:
            _snaps.pop(next(iter(_snaps)))
    return table


def session_files(data_dir: str = DATA_DIR, sym: str = 'SPX') -> list:
    """[(date, path)] of the full-chain day files, oldest first (gzipped or not)."""
    by_day = {}
    for p in sorted(glob.glob(os.path.join(data_dir, f'gex_full_{sym}_2*.jsonl*'))):
        by_day[os.path.basename(p)[len(f'gex_full_{sym}_'):][:10]] = p
    return sorted(by_day.items())


def positioning(exp_day: str, data_dir: str = DATA_DIR, band: float = 150.0) -> dict:
    """Open interest and GEX per strike for the expiration `exp_day` at the close of each
    recorded session it was listed in (today: the latest snapshot), strikes within `band`
    pts of the latest spot."""
    sessions = []
    for day, path in session_files(data_dir):
        if day > exp_day:
            break
        t = _file_table(path)
        if t and exp_day in t['exp']:
            sessions.append((day, t))
    if not sessions:
        return {'strikes': [], 'sessions': []}
    spot = sessions[-1][1]['spot']
    strikes = sorted({k for _, t in sessions for k in t['exp'][exp_day] if abs(k - spot) <= band})
    out = []
    for day, t in sessions:
        cells = t['exp'][exp_day]
        none = [0, 0, 0.0]
        atm = t.get('atm', {}).get(exp_day) or {}
        out.append({'day': day, 'ts': t['ts'], 'spot': t['spot'],
                    'call_oi': [cells.get(k, none)[0] for k in strikes],
                    'put_oi': [cells.get(k, none)[1] for k in strikes],
                    'gex': [round(cells.get(k, none)[2]) for k in strikes],
                    'total_gex': round(sum(c[2] for c in cells.values())),
                    'total_oi': sum(c[0] + c[1] for c in cells.values()),
                    'straddle': atm.get('straddle'), 'atm_iv': atm.get('iv'), 'atm_strike': atm.get('strike'),
                    'walls_30d': t.get('walls_30d', []), 'walls_week': t.get('walls_week', []),
                    **expiry_stats(cells, t['spot'])})
    return {'strikes': strikes, 'sessions': out}


# ── note context ───────────────────────────────────────────────────────────────

def trade_sides(day: str, data_dir: str = DATA_DIR, expiries=None) -> dict:
    """Each contract's volume that day by where it printed against the quote, from the
    minute chain snapshots (data/option_chain_SPX_<day>.csv, 30-day chain, no 0DTE):
    {(expiry, strike, 'c'|'p'): [at/near the ask (bought), at/near the bid (sold), between]}.
    An estimate: each minute's last trade stands for that minute's volume."""
    path = day_file(f'option_chain_SPX_{day}.csv', data_dir)
    if not path:
        return {}
    st = os.stat(path)
    sig = (st.st_size, st.st_mtime)
    key = (path, frozenset(expiries) if expiries else None)
    with _lock:
        hit = _sides.get(key)
        if hit and hit[0] == sig:
            return hit[1]
    seq = {}
    with open_text(path) as fh:
        rd = csv.reader(fh)
        ix = {n: i for i, n in enumerate(next(rd, []))}
        for row in rd:
            try:
                exp = row[ix['expiry']]
                if expiries and exp not in expiries:
                    continue
                seq.setdefault((exp, float(row[ix['strike']]), row[ix['side']][0]), []).append(
                    (int(row[ix['time']]), float(row[ix['bid']] or 0), float(row[ix['ask']] or 0),
                     float(row[ix['last']] or 0), int(float(row[ix['volume']] or 0))))
            except (ValueError, IndexError, KeyError):
                continue
    out = {}
    for k, rows in seq.items():
        rows.sort()
        b = sl = m = 0
        for prev, cur in zip(rows, rows[1:]):
            dv = cur[4] - prev[4]
            if dv <= 0:
                continue
            bid, ask, last = cur[1], cur[2], cur[3]
            spread = ask - bid
            if spread <= 0 or not last:
                m += dv
            elif last >= ask - 0.25 * spread:
                b += dv
            elif last <= bid + 0.25 * spread:
                sl += dv
            else:
                m += dv
        if b + sl + m:
            out[k] = [b, sl, m]
    with _lock:
        _sides[key] = (sig, out)
        while len(_sides) > 40:
            _sides.pop(next(iter(_sides)))
    return out


def session_ohlc(day: str) -> dict | None:
    """SPX open / high / low / close for a session from 1-minute candles; 'complete' once
    the 15:59 bar is in (finished sessions are cached)."""
    with _lock:
        if day in _ohlc:
            return _ohlc[day]
    d = date.fromisoformat(day)
    a = int(datetime.combine(d, dtime(9, 30), ET).timestamp() * 1000)
    b = int(datetime.combine(d, dtime(16, 0), ET).timestamp() * 1000)
    bars = load_range('$SPX', a, b)
    if not bars:
        return None
    o = {'open': bars[0]['open'], 'high': max(x['high'] for x in bars), 'low': min(x['low'] for x in bars),
         'close': bars[-1]['close'], 'complete': bars[-1]['datetime'] >= b - 60_000}
    if o['complete']:
        with _lock:
            _ohlc[day] = o
    return o


def next_session(day: str, today: str, path: str = EVENTS_CSV) -> str | None:
    """The trading session after `day`, if it has started (<= today)."""
    holidays = _load_events(path)['holidays']
    d = date.fromisoformat(day)
    for _ in range(7):
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in holidays:
            return d.isoformat() if d.isoformat() <= today else None
    return None


def level_result(level: float, spot_then: float, o: dict) -> tuple:
    """How the next session treated a level named in a note: ('broke' | 'held' |
    'not reached', how)."""
    if level < spot_then:
        if o['open'] <= level:
            return 'broke', f'opened below it at {o["open"]:.0f}'
        if o['low'] > level:
            return 'not reached', f'low {o["low"]:.0f}'
        if o['close'] < level:
            return 'broke', f'closed below it at {o["close"]:.0f}'
        return 'held', f'bounced, low {o["low"]:.0f}'
    if o['open'] >= level:
        return 'broke', f'opened above it at {o["open"]:.0f}'
    if o['high'] < level:
        return 'not reached', f'high {o["high"]:.0f}'
    if o['close'] > level:
        return 'broke', f'closed above it at {o["close"]:.0f}'
    return 'held', f'rejected, high {o["high"]:.0f}'


def event_history(category: str, before: str, path: str = EVENTS_CSV) -> dict | None:
    """How SPX moved on earlier days of that event type in the calendar: median absolute
    close-to-close move (%) and median high-low range (pts), from daily bars."""
    with open(path, encoding='utf-8', newline='') as f:
        dates = sorted({r['date'] for r in csv.DictReader(f) if r.get('category') == category and r['date'] < before})
    if not dates:
        return None
    start = datetime.combine(date.fromisoformat(dates[0]) - timedelta(days=10), dtime(0, 0), ET)
    bars = load_range('$SPX', int(start.timestamp() * 1000), None, interval='1d')
    by_day = {datetime.fromtimestamp(x['datetime'] / 1000, ET).date().isoformat(): x for x in bars}
    days = sorted(by_day)
    moves, ranges = [], []
    for d in dates:
        if d in by_day and days.index(d) > 0:
            prev, x = by_day[days[days.index(d) - 1]], by_day[d]
            moves.append(abs(x['close'] / prev['close'] - 1) * 100)
            ranges.append(x['high'] - x['low'])
    if not moves:
        return None
    return {'n': len(moves), 'move_pct': round(statistics.median(moves), 2), 'range': round(statistics.median(ranges))}


def event_info(day: str, tags: list, today: str, path: str = EVENTS_CSV) -> dict:
    """The event's name and time, and how SPX moved on earlier days of the same type."""
    cat = TAG_CATEGORY.get(tags[0]) if tags else None
    with open(path, encoding='utf-8', newline='') as f:
        row = next((r for r in csv.DictReader(f) if r['date'] == day and r.get('category') == cat), None)
    hist = event_history(cat, today, path) if cat else None
    txt = (row['event'] if row else ' / '.join(tags)) + (f" at {row['time_et']} ET" if row and row.get('time_et') else '')
    if hist:
        txt += (f" · earlier {' / '.join(tags).lower()} days this year: median move ±{hist['move_pct']:.2f}%,"
                f" median range {hist['range']} pts ({hist['n']} days)")
    return {'text': txt, 'time': row.get('time_et') if row else None, 'history': hist}


def _oi(v: float, sign: bool = False) -> str:
    a = abs(v)
    txt = f'{a / 1e6:.2f}M' if a >= 1e6 else f'{a / 1e3:.1f}K' if a >= 1e3 else str(int(a))
    return (('+' if v > 0 else '-' if v < 0 else '') if sign else ('-' if v < 0 else '')) + txt


def _gx(v: float) -> str:
    return ('+' if v >= 0 else '-') + (f'{abs(v) / 1e9:.2f}B' if abs(v) >= 1e9 else f'{abs(v) / 1e6:.1f}M')


def _wd(day: str) -> str:
    return datetime.strptime(day, '%Y-%m-%d').strftime('%a %m/%d')


def tally(rows: list) -> dict:
    """Outcome counts of the levels named in saved / built notes: {'neg': {...}, 'pos': {...}}
    (negative / positive gamma at the level) of broke / held / not reached."""
    out = {'neg': {'broke': 0, 'held': 0, 'not reached': 0}, 'pos': {'broke': 0, 'held': 0, 'not reached': 0}}
    for r in rows:
        oc = r.get('outcome') or {}
        if not oc.get('complete'):
            continue
        for side in ('below', 'above'):
            lv, res = (r.get('levels') or {}).get(side), oc.get(side)
            if lv and res:
                out['neg' if lv['gex'] < 0 else 'pos'][res] += 1
    return out


def tally_text(t: dict) -> str:
    f = lambda d: f"broke {d['broke']} · held {d['held']} · not reached {d['not reached']}"
    return f"negative-gamma levels {f(t['neg'])}; positive-gamma levels {f(t['pos'])}"


def build_notes(exp: str, tags: list, pos: dict, now: datetime | None = None, data_dir: str = DATA_DIR,
                path: str = EVENTS_CSV, near: float = 100.0, top: int = 3) -> tuple:
    """(notes newest first, rows oldest first, scorecard) for one expiration's sessions.
    notes: [{'day', 'lines': [{'text', 'cls'}]}]; rows: the numbers behind each note."""
    now = now or datetime.now(ET)
    today = now.date().isoformat()
    ks, ss = pos['strikes'], pos['sessions']
    reds = frozenset(red_days(path))
    notes, rows, lead = [], [], {}
    for i, s in enumerate(ss):
        p = ss[i - 1] if i else None
        spot = s['spot']
        idx = [j for j, k in enumerate(ks) if abs(k - spot) <= near]
        row = {'exp': exp, 'tags': tags, 'day': s['day'], 'ts': s['ts'], 'spot': spot,
               'total_oi': s['total_oi'], 'd_oi': s['total_oi'] - p['total_oi'] if p else None,
               'total_gex': s['total_gex'], 'straddle': s.get('straddle'), 'atm_iv': s.get('atm_iv'),
               'max_pain': s.get('max_pain'), 'flip': s.get('flip'), 'top_oi': s.get('top_oi'),
               'walls_30d': s.get('walls_30d'), 'walls_week': s.get('walls_week')}
        lines = [{'text': f"OI {_oi(s['total_oi'])}" + (f" ({_oi(row['d_oi'], True)})" if p else '')
                          + f" · GEX {_gx(s['total_gex'])}" + (f" (was {_gx(p['total_gex'])})" if p else '')
                          + f" · spot {spot:.0f}" + (f" ({spot - p['spot']:+.0f})" if p else '')}]
        sd = s.get('straddle')
        if sd:
            iv = s.get('atm_iv')
            ivtxt = (f", IV {iv:.1f}%" + (f", was {p['atm_iv']:.1f}%" if p and p.get('atm_iv') else '')) if iv else ''
            lines.append({'cls': 'move', 'text': f"Priced move to {_wd(exp)}: ±{sd:.0f} pts → {spot - sd:.0f}–{spot + sd:.0f}"
                                                 f" (ATM straddle ${sd:.2f}{ivtxt})"})
        if p:
            # open interest reported this morning is the previous session's trading: who traded then
            sides = trade_sides(p['day'], data_dir, reds)
            def who(k, sd_):
                v = sides.get((exp, k, sd_.lower()))
                if not v:
                    return '', None
                b, sl = v[0] / sum(v), v[1] / sum(v)
                return f"{_wd(p['day'])[:3]} trades {b:.0%} bought / {sl:.0%} sold", round(b, 2)
            ch = [(s['call_oi'][j] - p['call_oi'][j], ks[j], 'C', s['call_oi'][j]) for j in idx] + \
                 [(s['put_oi'][j] - p['put_oi'][j], ks[j], 'P', s['put_oi'][j]) for j in idx]
            grow = sorted([c for c in ch if c[0] > 0], key=lambda c: -c[0])[:top]
            shrink = sorted([c for c in ch if c[0] < 0], key=lambda c: c[0])[:2]
            gtxt, row['growing'] = [], []
            for d, k, sd_, now_ in grow:
                w, bought = who(k, sd_)
                gtxt.append(f"{k:g}{sd_} {_oi(d, True)} (now {_oi(now_)}{'; ' + w if w else ''})")
                row['growing'].append({'strike': k, 'side': sd_, 'change': d, 'oi': now_, 'bought': bought})
            row['shrinking'] = [{'strike': k, 'side': sd_, 'change': d} for d, k, sd_, _ in shrink]
            lines.append({'cls': 'grow', 'text': 'Growing: ' + (', '.join(gtxt) or 'no new positions in size')})
            lines.append({'cls': 'shrink', 'text': 'Shrinking: ' + (', '.join(f"{k:g}{sd_} {_oi(d)}" for d, k, sd_, _ in shrink)
                                                                    or 'nothing closed in size')})
        row['levels'] = {}
        for side, name, on in (('below', 'Biggest gamma below spot', lambda k: k < spot),
                               ('above', 'Biggest gamma above spot', lambda k: k > spot)):
            js = [j for j in idx if on(ks[j])]
            if not js:
                continue
            j = max(js, key=lambda j: abs(s['gex'][j]))
            k = ks[j]
            if lead.get(side, (None,))[0] != k:
                lead[side] = (k, s['day'])
            extra = ['first recorded' if not i else 'new today' if lead[side][1] == s['day'] else f'there since {_wd(lead[side][1])}']
            if sd:
                extra.append(f"{abs(k - spot) / sd:.1f}× the priced move away")
            also = [n for n, w in (('30-day', s.get('walls_30d') or []), ('Week', s.get('walls_week') or [])) if k in w]
            if also:
                extra.append('also a ' + ' and '.join(also) + ' wall')
            lines.append({'cls': 'level', 'text': f"{name}: {k:g} {_gx(s['gex'][j])} ({'; '.join(extra)})"})
            row['levels'][side] = {'strike': k, 'gex': s['gex'][j], 'since': lead[side][1], 'also': also}
        mag = []
        if s.get('max_pain') is not None:
            mag.append(f"max pain {s['max_pain']:g}")
        if s.get('top_oi'):
            mag.append('biggest OI ' + ', '.join(f'{k:g}' for k in s['top_oi']))
        mag.append(f"gamma flip {s['flip']:g}" if s.get('flip') is not None else 'no gamma flip')
        lines.append({'cls': 'magnet', 'text': 'Magnets: ' + ' · '.join(mag)})
        lines.append({'text': 'Dealers short gamma in this expiration: their hedging adds to moves' if s['total_gex'] < 0
                      else 'Dealers long gamma in this expiration: their hedging damps moves'})
        # what SPX did next, against the levels above
        if s['day'] == exp:
            o = session_ohlc(exp)
            if o:
                mp = f" · max pain was {s['max_pain']:g} ({o['close'] - s['max_pain']:+.0f})" if s.get('max_pain') is not None else ''
                lines.append({'cls': 'outcome', 'text': f"Expiration day: SPX {'settled' if o['complete'] else 'at'} {o['close']:.0f}{mp}"})
                row['outcome'] = {'day': exp, **o}
        else:
            nd = next_session(s['day'], today, path)
            o = session_ohlc(nd) if nd else None
            if not o:
                lines.append({'cls': 'outcome', 'text': 'Next session: still to come'})
            else:
                res = []
                row['outcome'] = {'day': nd, **o}
                for side, lv in row['levels'].items():
                    r, how = level_result(lv['strike'], spot, o)
                    row['outcome'][side] = r
                    res.append(f"{lv['strike']:g} {r} ({how})")
                lines.append({'cls': 'outcome', 'text': f"Next session ({_wd(nd)}{'' if o['complete'] else ', so far'}):"
                                                        f" O {o['open']:.0f} H {o['high']:.0f} L {o['low']:.0f} C {o['close']:.0f}"
                                                        + (' · ' + ' · '.join(res) if res else '')})
        notes.append({'day': s['day'], 'lines': lines})
        rows.append(row)
    return notes[::-1], rows, tally(rows)


def save_notes(rows: list, path: str | None = None) -> list:
    """Merge rows into the notes file (one per expiration and session, newest wins) and
    return everything in it. Rewritten only when something changed. Serialised: two page
    requests saving at once collided on Windows (os.replace on a file the other had open)."""
    with _notes_lock:
        return _save_notes(rows, path or NOTES_FILE)


def _save_notes(rows: list, path: str) -> list:
    have = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    r = json.loads(line)
                    have[(r['exp'], r['day'])] = r
                except (ValueError, KeyError):
                    continue
    changed = False
    for r in rows:
        r = json.loads(json.dumps(r))                       # as it will read back (tuples -> lists)
        if have.get((r['exp'], r['day'])) != r:
            have[(r['exp'], r['day'])] = r
            changed = True
    if changed:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            for key in sorted(have):
                f.write(json.dumps(have[key], separators=(',', ':')) + '\n')
        os.replace(tmp, path)
    return list(have.values())


def save_red_day_notes(now: datetime, data_dir: str = DATA_DIR, path: str = EVENTS_CSV,
                       notes_file: str | None = None, built: dict | None = None, ahead_days: int = 45) -> list:
    """Notes of every red day within ahead_days, finished sessions only (outcomes fill in
    the next day), into the notes file. Recomputed when a new full-chain snapshot lands."""
    files = session_files(data_dir)
    if not files:
        return []
    st = os.stat(files[-1][1])
    after_close = now.time() >= dtime(16, 0)
    sig = (files[-1][1], st.st_size, st.st_mtime, now.date().isoformat(), after_close, notes_file)
    if _notes_saved['sig'] == sig:
        return _notes_saved['rows']
    today, until = now.date().isoformat(), (now.date() + timedelta(days=ahead_days)).isoformat()
    finished = lambda day: day < today or (day == today and after_close)
    rows = []
    for d, tags in sorted(red_days(path).items()):
        if today <= d <= until:
            r = (built or {}).get(d)
            if r is None:
                _, r, _ = build_notes(d, tags, positioning(d, data_dir), now, data_dir, path)
            rows += [x for x in r if finished(x['day'])]
    saved = save_notes(rows, notes_file)
    _notes_saved.update(sig=sig, rows=saved)
    return saved


def summary(n: int = 2, now: datetime | None = None, data_dir: str = DATA_DIR,
            path: str = EVENTS_CSV, ahead_days: int = 130, notes_file: str | None = None) -> dict:
    """Red days for the next ~4 months; positioning and notes for the next n of them; the
    scorecard of every saved note."""
    now = now or datetime.now(ET)
    today = now.date().isoformat()
    until = (now.date() + timedelta(days=ahead_days)).isoformat()
    red = {d: t for d, t in red_days(path).items() if today <= d <= until}
    nxt, built = [], {}
    for d, tags in next_red_days(n, now, path):
        pos = positioning(d, data_dir)
        notes, rows, card = build_notes(d, tags, pos, now, data_dir, path)
        built[d] = rows
        nxt.append({'date': d, 'tags': tags, 'sessions_away': sessions_away(today, d, path), **pos,
                    'notes': notes, 'scorecard': tally_text(card), 'event': event_info(d, tags, today, path)})
    saved = save_red_day_notes(now, data_dir, path, notes_file, built)
    return {'red': red, 'next': nxt, 'scorecard_all': tally_text(tally(saved)),
            'notes_saved': len(saved)}
