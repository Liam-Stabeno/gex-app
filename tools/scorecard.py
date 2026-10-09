"""
Summarise data/scorecard_SPX.csv: how often each level did its job.

    python tools/scorecard.py              # all days
    python tools/scorecard.py --last 20    # most recent 20 days
    python tools/scorecard.py --rebuild    # recompute every day's row first

The dashboard writes one scorecard row per day at 16:20 ET (background.daily_jobs_loop).
Small samples mislead: treat anything under ~20 days as anecdotal.
"""
import argparse
import os
import sys
from datetime import datetime

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
import gex_stats  # noqa: E402

PATH = os.path.join(ROOT, 'data', 'scorecard_SPX.csv')


def rate(df, touch, brk):
    t = pd.to_numeric(df[touch], errors='coerce')
    b = pd.to_numeric(df[brk], errors='coerce')
    touched = t == 1
    n = int(touched.sum())
    held = int((touched & (b == 0)).sum())
    return f'touched {n:3d} days, held {held:3d} ({held / n:.0%})' if n else 'never touched'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--last', type=int, default=0)
    ap.add_argument('--rebuild', action='store_true')
    a = ap.parse_args()

    if a.rebuild:
        names = [f for f in os.listdir(os.path.join(ROOT, 'data'))
                 if f.startswith('gex_snapshots_SPX_') and f.endswith('.csv')]
        today = datetime.now(gex_stats.ET).date()
        for d in sorted(datetime.strptime(f[-14:-4], '%Y-%m-%d').date() for f in names):
            if d < today or datetime.now(gex_stats.ET).hour >= 16:   # today only once it closed
                gex_stats.write_scorecard(d)
    if not os.path.exists(PATH):
        sys.exit('No scorecard yet: it is written after the close, or run with --rebuild.')

    df = pd.read_csv(PATH, dtype=str)
    if a.last:
        df = df.tail(a.last)
    num = lambda c: pd.to_numeric(df[c], errors='coerce')
    print(f'Scorecard: {len(df)} days ({df["date"].min()} to {df["date"].max()})\n')

    print('Walls: when price came within 3 pts, did the wall hold (no 1-min close 2+ pts through)?')
    for name, (t, b) in {'Resist (biggest wall above)': ('resist_touch', 'resist_break'),
                         'Support (biggest wall below)': ('support_touch', 'support_break'),
                         'Call wall (ex-0DTE)': ('call_wall_touch', 'call_wall_break'),
                         'Put wall (ex-0DTE)': ('put_wall_touch', 'put_wall_break')}.items():
        print(f'  {name:30s} {rate(df, t, b)}')
    for name, gap, brk in (('Resist approached within 10 pts', 'resist_gap_at_high', 'resist_break'),
                           ('Support approached within 10 pts', 'support_gap_at_low', 'support_break')):
        gp, bk = num(gap), num(brk)
        near = gp <= 10
        k, held = int(near.sum()), int((near & (bk == 0)).sum())
        print(f'  {name:30s} ' + (f'{k:3d} days, held {held:3d} ({held / k:.0%})' if k else 'never'))
    tb = num('trapdoor_break').dropna()
    print(f'  {"Trapdoor broken":30s} {int(tb.sum())} of {len(tb)} days')

    # Not the 16:00 value: by then 0DTE gamma sits on the strike nearest price, so a
    # final pin is near the close by construction.
    print('\nPins: median distance from the 16:00 close of the pin in force at each time (pts)')
    for c, label in (('true_pin_dist_1400', 'True Pin at 14:00'), ('pin_0dte_dist_1400', 'Pin 0DTE at 14:00'),
                     ('true_pin_dist_1500', 'True Pin at 15:00'), ('pin_0dte_dist_1500', 'Pin 0DTE at 15:00'),
                     ('true_pin_dist_1530', 'True Pin at 15:30'), ('pin_0dte_dist_1530', 'Pin 0DTE at 15:30'),
                     ('pin_lt_dist', 'Pin LT (final)')):
        v = num(c).dropna()
        print(f'  {label:20s} ' + (f'{v.median():6.1f}  (within 10 pts on {(v <= 10).mean():.0%} of {len(v)} days)' if len(v) else 'no data'))

    em = num('em_1400_in_p80').dropna()
    em_m = num('em_1400_in_median').dropna()
    print(f'\nExpected move from 14:00: inside median {em_m.mean():.0%}, inside 80% band {em.mean():.0%} ({len(em)} days)'
          if len(em) else '\nExpected move: no data')

    r = num('speed_ratio').dropna()
    hs, hn = num('speed_hours_slower').sum(), num('speed_hours').sum()
    print(f'Heatmap: price moved {r.median():.2f}x faster in dim bands than bright (median, {len(r)} days); '
          f'within the same hour, high GEX was slower in {int(hs)} of {int(hn)} hours ({hs / hn:.0%})'
          if len(r) and hn else 'Heatmap: no data')
    # The brake is a positive-gamma effect; in negative gamma dealers chase the move.
    for tag, label in (('pos', 'positive'), ('neg', 'negative')):
        v = num(f'speed_ratio_{tag}').dropna()
        if len(v):
            print(f'  in {label}-gamma minutes: {v.median():.2f}x (median, {len(v)} days; >1 = slower in bright bands)')
    ns = num('neg_share').dropna()
    if len(ns):
        print(f'  days mostly in negative gamma: {int((ns >= 0.5).sum())} of {len(ns)}')

    ch, cc = num('charm_hits').sum(), num('charm_calls').sum()
    print(f'Charm flow: sign matched the move into the close {int(ch)} of {int(cc)} times ({ch / cc:.0%})'
          if cc else 'Charm flow: no data yet (saved from 2026-10-07)')


if __name__ == '__main__':
    main()
