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
"""
import csv
import glob
import json
import os
import threading
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import bs
from gex_stats import open_text

ET = ZoneInfo('America/New_York')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_CSV = os.path.join(ROOT, 'reference', 'market_events.csv')
DATA_DIR = os.path.join(ROOT, 'data')
RISK_FREE = 0.05            # background._RISK_FREE
SNAP_CACHE_MAX = 30         # parsed full-chain snapshots kept (one per session file)

_lock = threading.Lock()
_events = {'sig': None, 'red': {}, 'holidays': set()}
_snaps: dict = {}           # file path -> ((size, mtime), table)


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
    """{'ts', 'spot', 'exp': {expiry: {strike: [call_oi, put_oi, gex]}}} from one snapshot.
    GEX = gamma x OI x 100 x spot, calls +, puts -, gamma from the contract's vol."""
    when = datetime.fromisoformat(snap['ts'])
    spot = float(snap['spot'])
    f = {name: i for i, name in enumerate(snap['fields'])}
    t_by_exp, out = {}, {}
    for r in snap['rows']:
        oi = int(r[f['oi']] or 0)
        if oi <= 0:
            continue
        exp, k = snap['exp'][r[f['exp_idx']]], float(r[f['strike']])
        if exp not in t_by_exp:
            t_by_exp[exp] = bs.t_to_close(exp, now=when)
        T, iv = t_by_exp[exp], r[f['iv']]
        g = bs._bs_gamma(spot, k, T, RISK_FREE, float(iv) / 100) if iv and 0 < float(iv) < 500 and T > 0 else 0.0
        call = r[f['side']] == 0
        cell = out.setdefault(exp, {}).setdefault(k, [0, 0, 0.0])
        cell[0 if call else 1] += oi
        cell[2] += (g if call else -g) * oi * 100 * spot
    return {'ts': snap['ts'], 'spot': spot, 'exp': out}


def _file_table(path: str) -> dict | None:
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
        out.append({'day': day, 'ts': t['ts'], 'spot': t['spot'],
                    'call_oi': [cells.get(k, none)[0] for k in strikes],
                    'put_oi': [cells.get(k, none)[1] for k in strikes],
                    'gex': [round(cells.get(k, none)[2]) for k in strikes],
                    'total_gex': round(sum(c[2] for c in cells.values())),
                    'total_oi': sum(c[0] + c[1] for c in cells.values())})
    return {'strikes': strikes, 'sessions': out}


def summary(n: int = 2, now: datetime | None = None, data_dir: str = DATA_DIR,
            path: str = EVENTS_CSV, ahead_days: int = 130) -> dict:
    """Red days for the next ~4 months, and positioning into the next n of them."""
    now = now or datetime.now(ET)
    today = now.date().isoformat()
    until = (now.date() + timedelta(days=ahead_days)).isoformat()
    red = {d: t for d, t in red_days(path).items() if today <= d <= until}
    nxt = [{'date': d, 'tags': tags, 'sessions_away': sessions_away(today, d, path),
            **positioning(d, data_dir)} for d, tags in next_red_days(n, now, path)]
    return {'red': red, 'next': nxt}
