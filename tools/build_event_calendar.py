"""
Build reference/market_events.csv: scheduled events a model should know about (they
change how a day trades), joined to recorded data by date.

    python tools/build_event_calendar.py

Official dates (fetched 2026-10-13):
  FOMC   https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm  (2027 tentative)
  CPI    https://www.bls.gov/schedule/2026/home.htm, https://www.bls.gov/schedule/news_release/cpi.htm
  Jobs   https://www.bls.gov/schedule/2026/home.htm, https://www.bls.gov/schedule/news_release/empsit.htm
  NYSE   https://www.nyse.com/markets/hours-calendars
Computed: monthly / quarterly option expiration (3rd Friday; the day before if that's a
holiday), VIX expiration (the Wednesday 30 days before the next month's SPX expiration),
last trading day of each month / quarter.
Add the next year's official dates to the lists below when they're published.
"""
import csv
import os
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'reference', 'market_events.csv')
YEARS = (2026, 2027)

FED = 'federalreserve.gov/monetarypolicy/fomccalendars.htm'
BLS = 'bls.gov release schedule'
NYSE = 'nyse.com/markets/hours-calendars'

# FOMC decision day (the meeting's second day), True = with Summary of Economic Projections
FOMC = {date(2026, 1, 28): False, date(2026, 3, 18): True, date(2026, 4, 29): False,
        date(2026, 6, 17): True, date(2026, 7, 29): False, date(2026, 9, 16): True,
        date(2026, 10, 28): False, date(2026, 12, 9): True,
        date(2027, 1, 27): False, date(2027, 3, 17): True, date(2027, 4, 28): False,
        date(2027, 6, 9): True, date(2027, 7, 28): False, date(2027, 9, 15): True,
        date(2027, 10, 27): False, date(2027, 12, 8): True}

CPI = [date(2026, 1, 13), date(2026, 2, 13), date(2026, 3, 11), date(2026, 4, 10), date(2026, 5, 12),
       date(2026, 6, 10), date(2026, 7, 14), date(2026, 8, 12), date(2026, 9, 11), date(2026, 10, 14),
       date(2026, 11, 10), date(2026, 12, 10), date(2027, 1, 13), date(2027, 2, 11), date(2027, 3, 10),
       date(2027, 4, 13), date(2027, 5, 12), date(2027, 6, 10), date(2027, 7, 14), date(2027, 8, 11),
       date(2027, 9, 14), date(2027, 10, 14), date(2027, 11, 10), date(2027, 12, 10)]

JOBS = [date(2026, 1, 9), date(2026, 2, 11), date(2026, 3, 6), date(2026, 4, 3), date(2026, 5, 8),
        date(2026, 6, 5), date(2026, 7, 2), date(2026, 8, 7), date(2026, 9, 4), date(2026, 10, 2),
        date(2026, 11, 6), date(2026, 12, 4), date(2027, 1, 8), date(2027, 2, 5), date(2027, 3, 5),
        date(2027, 4, 2), date(2027, 5, 7), date(2027, 6, 4), date(2027, 7, 2), date(2027, 8, 6),
        date(2027, 9, 3), date(2027, 10, 8), date(2027, 11, 5), date(2027, 12, 3)]

HOLIDAYS = {date(2026, 1, 1): "New Year's Day", date(2026, 1, 19): 'Martin Luther King Jr. Day',
            date(2026, 2, 16): "Washington's Birthday", date(2026, 4, 3): 'Good Friday',
            date(2026, 5, 25): 'Memorial Day', date(2026, 6, 19): 'Juneteenth',
            date(2026, 7, 3): 'Independence Day (observed)', date(2026, 9, 7): 'Labor Day',
            date(2026, 11, 26): 'Thanksgiving', date(2026, 12, 25): 'Christmas',
            date(2027, 1, 1): "New Year's Day", date(2027, 1, 18): 'Martin Luther King Jr. Day',
            date(2027, 2, 15): "Washington's Birthday", date(2027, 3, 26): 'Good Friday',
            date(2027, 5, 31): 'Memorial Day', date(2027, 6, 18): 'Juneteenth (observed)',
            date(2027, 7, 5): 'Independence Day (observed)', date(2027, 9, 6): 'Labor Day',
            date(2027, 11, 25): 'Thanksgiving', date(2027, 12, 24): 'Christmas (observed)'}

EARLY_CLOSE = {date(2026, 11, 27): 'Day after Thanksgiving', date(2026, 12, 24): 'Christmas Eve',
               date(2027, 11, 26): 'Day after Thanksgiving'}


def trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def prev_trading_day(d: date) -> date:
    while not trading_day(d):
        d -= timedelta(days=1)
    return d


def third_friday(y: int, m: int) -> date:
    d = date(y, m, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def build() -> list:
    rows = []
    add = lambda d, t, ev, cat, imp, src: rows.append([d.isoformat(), t, ev, cat, imp, src])
    for d, sep in FOMC.items():
        add(d, '14:00', 'FOMC decision' + (' + projections (SEP)' if sep else ''), 'fomc', 'high', FED)
    # BLS doesn't follow the NYSE calendar (the April 2026 jobs report came out on Good
    # Friday): the reaction is then in ES only
    shut = lambda d: '' if trading_day(d) else ' (stock market closed; ES trades)'
    for d in CPI:
        add(d, '08:30', 'CPI' + shut(d), 'cpi', 'high', BLS)
    for d in JOBS:
        add(d, '08:30', 'Jobs report (Employment Situation)' + shut(d), 'jobs', 'high', BLS)
    for d, name in HOLIDAYS.items():
        add(d, '', f'Market closed: {name}', 'holiday', 'high', NYSE)
    for d, name in EARLY_CLOSE.items():
        add(d, '13:00', f'Early close 1:00 PM (options 1:15): {name}', 'early_close', 'high', NYSE)
    for y in YEARS:
        for m in range(1, 13):
            exp = prev_trading_day(third_friday(y, m))
            quad = m in (3, 6, 9, 12)
            add(exp, '', 'Quarterly expiration (quad witching)' if quad else 'Monthly option expiration',
                'opex', 'high' if quad else 'medium', 'computed')
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            vix = prev_trading_day(third_friday(ny, nm) - timedelta(days=30))
            add(vix, '09:30', 'VIX expiration (settles on SPX option prices at the open)', 'vix_exp',
                'medium', 'computed')
            last = prev_trading_day((date(ny, nm, 1) - timedelta(days=1)))
            add(last, '', 'Quarter end' if quad else 'Month end', 'period_end',
                'medium' if quad else 'low', 'computed')
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def main():
    rows = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['date', 'time_et', 'event', 'category', 'importance', 'source'])
        w.writerows(rows)
    print(f'{len(rows)} events -> {OUT}')


if __name__ == '__main__':
    main()
