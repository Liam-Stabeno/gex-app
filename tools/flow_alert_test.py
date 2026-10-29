"""
Flow alerts as a signal: after a big option print the app labelled BUY or SELL (the side
the customer took), does SPX move that way next?

Signal: BUY call / SELL put -> up;  BUY put / SELL call -> down. Unlabelled prints only
count in the size test.
  first to 5   SPX gets 5 pts the signal's way before 5 pts the other way, within 30 / 60
               min of the close of the alert's minute; vs the same day's rate over every
               minute (fair: a busy minute moves more both ways)
  drift        SPX points the signal's way 5 / 15 / 30 / 60 min later
  size         |SPX move| over the next 15 min after any alert vs the same day's minutes
               within 30 min of it (time of day matters: the open moves more)
  option P&L   0DTE from 2026-10-14: BUY alerts -> the same contract bought at the ask at
               the end of the alert's minute, sold at the bid 15 / 30 / 60 min later;
               any labelled alert -> the ATM 0DTE call / put its way. Baseline: ATM calls
               and puts bought every 15 min on the same days
Events: an alert within 5 min of the last counted one the same way is skipped (bursts
are one decision); 'all' counts every alert.
Labels before 2026-10-29 were judged on the quote at the trade (spread legs could both
read BUY); from 10/29 on the quote before it, so the two are reported apart.

Run:  python tools/flow_alert_test.py
"""
import glob
import gzip
import json
import math
import os
import statistics
import sys
import time
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = r'C:\Productivity\TradingApp'
sys.path.insert(0, ROOT + r'\src')
os.chdir(ROOT)
import predictions as P  # noqa: E402

ET = ZoneInfo('America/New_York')
MOVE = 5.0
FIRST = (30, 60)
DRIFT = (5, 15, 30, 60)
EVENT_SEC = 300
NEW_LABELS = '2026-10-29'
OPT_FROM = '2026-10-14'
BASE_EVERY = 15


def load_alerts() -> list:
    out = []
    for f in sorted(glob.glob('data/flow_alerts_*.json*')):
        try:
            with (gzip.open(f, 'rt') if f.endswith('.gz') else open(f)) as fh:
                rows = json.load(fh)
        except (OSError, ValueError):
            continue
        for a in rows:
            if a.get('underlying', 'SPX') == 'SPX' and a.get('time'):
                a['day'] = datetime.fromtimestamp(a['time'], ET).date().isoformat()
                out.append(a)
    return sorted(out, key=lambda a: a['time'])


def signal(a) -> int:
    d, s = a.get('direction'), a.get('side')
    if d not in ('BUY', 'SELL') or s not in ('call', 'put'):
        return 0
    return 1 if (d == 'BUY') == (s == 'call') else -1


def first_to(closes, i, sign, minutes):
    c0 = closes[i]
    for j in range(i + 1, min(i + minutes + 1, len(closes))):
        d = sign * (closes[j] - c0)
        if d >= MOVE:
            return True
        if d <= -MOVE:
            return False
    return None


def ci(p, n):
    return 1.96 * math.sqrt(max(p * (1 - p), 1e-9) / n) if n else float('nan')


def mean_t(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 3:
        return float('nan'), float('nan'), len(xs)
    m = statistics.fmean(xs)
    sd = statistics.stdev(xs)
    return m, (m / (sd / math.sqrt(len(xs))) if sd else float('nan')), len(xs)


def main():
    sessions = P.sessions('SPX', max_age=0)
    alerts = load_alerts()
    now = time.time()
    print(f'{len(alerts)} SPX flow alerts on {len({a["day"] for a in alerts})} days, '
          f'{sum(1 for a in alerts if signal(a))} with a BUY / SELL label')

    # per day: closes, and the day's own rates (the baseline)
    day = {}
    for d in sorted({a['day'] for a in alerts}):
        bars = sessions.get(d)
        if not bars or len(bars) < 60:
            continue
        times, closes = [b[0] for b in bars], [b[4] for b in bars]
        base = {h: [first_to(closes, i, 1, h) for i in range(len(closes))] for h in FIRST}
        size15 = [abs(closes[i + 15] - closes[i]) if i + 15 < len(closes) else None for i in range(len(closes))]
        day[d] = dict(times=times, closes=closes, base=base, size15=size15)

    rows, last_kept = [], {}
    for a in alerts:
        D = day.get(a['day'])
        if not D:
            continue
        m = int(a['time']) // 60 * 60
        i = bisect_right(D['times'], m) - 1
        if i < 0 or D['times'][i] != m:
            continue                                      # outside the session's minutes
        s = signal(a)
        key = (a['day'], s)
        event = s == 0 or key not in last_kept or a['time'] - last_kept[key] > EVENT_SEC
        if s and event:
            last_kept[key] = a['time']
        c = D['closes']
        r = dict(a=a, i=i, m=m, s=s, event=event)
        if s:
            for h in FIRST:
                r[f'f{h}'] = first_to(c, i, s, h)
                b = [x for x in D['base'][h] if x is not None]
                up = sum(b) / len(b) if b else 0.5
                r[f'b{h}'] = up if s > 0 else 1 - up
            for h in DRIFT:
                r[f'd{h}'] = s * (c[i + h] - c[i]) if i + h < len(c) else None
        near = [x for j, x in enumerate(D['size15']) if x is not None and abs(j - i) <= 30]
        r['size'] = D['size15'][i]
        r['size_base'] = statistics.fmean(near) if near else None
        rows.append(r)

    big = statistics.median([abs(r['a'].get('weighted_delta') or 0) for r in rows if r['s']] or [0])
    groups = [
        ('all labelled', lambda r: True),
        ('BUY', lambda r: r['a']['direction'] == 'BUY'),
        ('SELL', lambda r: r['a']['direction'] == 'SELL'),
        ('BUY call', lambda r: r['a']['direction'] == 'BUY' and r['a']['side'] == 'call'),
        ('BUY put', lambda r: r['a']['direction'] == 'BUY' and r['a']['side'] == 'put'),
        ('0DTE', lambda r: r['a'].get('is_0dte')),
        ('later expiries', lambda r: not r['a'].get('is_0dte')),
        ('label certain', lambda r: r['a'].get('direction_certain') is True),
        (f'big (|delta| >= {big:,.0f})', lambda r: abs(r['a'].get('weighted_delta') or 0) >= big),
        ('before 12:00', lambda r: datetime.fromtimestamp(r['m'], ET).hour < 12),
        ('12:00 on', lambda r: datetime.fromtimestamp(r['m'], ET).hour >= 12),
        ('Jun-Sep', lambda r: r['a']['day'] < '2026-10-01'),
        ('Oct (to 10/28)', lambda r: '2026-10-01' <= r['a']['day'] < NEW_LABELS),
        ('new labels (10/29 on)', lambda r: r['a']['day'] >= NEW_LABELS),
    ]

    lab = [r for r in rows if r['s']]
    for title, pick in (('EVERY ALERT', lambda r: True), ('EVENTS (bursts counted once)', lambda r: r['event'])):
        print(f'\n{title}: SPX {MOVE:g} pts the signal\'s way first (vs the same day\'s rate), and drift in pts')
        print(f'  {"group":26s} {"n":>5s}  {"30m first":>17s} {"base":>5s}  {"60m first":>17s} {"base":>5s}   '
              + '  '.join(f'{"drift " + str(h) + "m":>13s}' for h in DRIFT))
        for name, g in groups:
            sub = [r for r in lab if pick(r) and g(r)]
            if len(sub) < 5:
                continue
            cells = []
            for h in FIRST:
                k = [r for r in sub if r[f'f{h}'] is not None]
                p = sum(r[f'f{h}'] for r in k) / len(k) if k else float('nan')
                b = statistics.fmean(r[f'b{h}'] for r in k) if k else float('nan')
                cells.append(f'{100 * p:5.1f}% ±{100 * ci(p, len(k)):4.1f} n{len(k):<4d} {100 * b:4.1f}%')
            drift = []
            for h in DRIFT:
                mu, t, n = mean_t([r[f'd{h}'] for r in sub])
                drift.append(f'{mu:+6.2f} t{t:+5.1f}')
            print(f'  {name:26s} {len(sub):5d}  {cells[0]}  {cells[1]}   ' + '  '.join(drift))

    print('\nSIZE: |SPX move| over the next 15 min, after an alert vs the same day within 30 min of it')
    for name, g in (('every alert', lambda r: True), ('labelled', lambda r: r['s']), ('unlabelled', lambda r: not r['s']),
                    ('0DTE', lambda r: r['a'].get('is_0dte')), ('later expiries', lambda r: not r['a'].get('is_0dte'))):
        sub = [r for r in rows if g(r) and r['size'] is not None and r['size_base']]
        if len(sub) < 5:
            continue
        ratio = [r['size'] / r['size_base'] for r in sub]
        mu, t, n = mean_t([r['size'] - r['size_base'] for r in sub])
        print(f'  {name:16s} n{len(sub):<5d} {statistics.fmean(r["size"] for r in sub):5.2f} pts vs '
              f'{statistics.fmean(r["size_base"] for r in sub):5.2f}  ratio of means '
              f'{sum(r["size"] for r in sub) / sum(r["size_base"] for r in sub):4.2f}x  '
              f'median ratio {statistics.median(ratio):4.2f}x  diff t{t:+.1f}')

    # ── 0DTE option P&L ────────────────────────────────────────────────────────
    odays = sorted(d for d in day if d >= OPT_FROM)
    base = defaultdict(list)
    same, atm = defaultdict(list), defaultdict(list)
    for d in odays:
        q = P.option_quotes(d)
        if not q:
            continue
        D = day[d]
        for i in range(BASE_EVERY, len(D['times']) - 60, BASE_EVERY):
            k = round(D['closes'][i] / 5) * 5
            for side in ('call', 'put'):
                tr = P._trade(q, side, k, D['times'][i], now)
                if tr:
                    base[side].append(tr)
        for r in rows:
            if r['a']['day'] != d or not r['s'] or not r['event']:
                continue
            a = r['a']
            if a['direction'] == 'BUY' and a.get('is_0dte'):
                tr = P._trade(q, a['side'], float(a['strike']), r['m'], now)
                if tr:
                    same[a['side']].append(tr)
            side = 'call' if r['s'] > 0 else 'put'
            tr = P._trade(q, side, round(D['closes'][r['i']] / 5) * 5, r['m'], now)
            if tr:
                atm[side].append(tr)

    def show(name, trades):
        cols = []
        for h in ('15', '30', '60'):
            mu, t, n = mean_t([x[h] for x in trades])
            cols.append(f'{h}m {mu:+6.1f}% t{t:+4.1f} n{n:<4d}')
        hits = [x['hit'] for x in trades if x['hit'] is not None]
        tp = mean_t([x['tp'] for x in trades])[0]
        hit = f'{100 * sum(hits) / len(hits):4.0f}%' if hits else '   -'
        print(f'  {name:34s} ' + '  '.join(cols) + f'   hit +25% {hit}  take-profit avg {tp:+5.1f}%')

    print(f'\n0DTE OPTION P&L, events only ({len(odays)} days from {OPT_FROM}; bought at the ask, sold at the bid)')
    for side in ('call', 'put'):
        show(f'baseline: ATM {side} every {BASE_EVERY} min', base[side])
    for side in ('call', 'put'):
        show(f'BUY alert: same {side} as the print', same[side])
    for side in ('call', 'put'):
        show(f'any label: ATM {side} its way', atm[side])


if __name__ == '__main__':
    main()
