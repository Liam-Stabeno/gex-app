"""Does the gamma customers bought (dealers' gamma falling) in the last 15 min say anything
about the SIZE of the next 15 min, beyond the size of the last 15 min? Per-minute cache from
tools/tick_flow_test.py (run it first)."""
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, r'C:\Productivity\TradingApp\src')
os.chdir(r'C:\Productivity\TradingApp')
import predictions as P  # noqa: E402

mins = pd.read_csv(os.path.join(tempfile.gettempdir(), 'tick_flow_minutes.csv'), index_col=0)
bars = P.sessions('SPX')
H = 15
rows = []
for d, m in mins.groupby('day'):
    b = pd.DataFrame(bars[d], columns=['t', 'o', 'h', 'l', 'c']).set_index('t')
    f = b.join(m[['odte_gamma', 'odte_stable', 'vol']], how='left').fillna(0.0)
    for i in range(H, len(f) - H, H):
        past, nxt = f.iloc[i - H:i], f.iloc[i:i + H]
        rows.append({'g': past['odte_gamma'].sum(), 'absflow': abs(past['odte_stable'].sum()),
                     'vol': past['vol'].sum(),
                     'rng_past': past['h'].max() - past['l'].min(), 'rng_next': nxt['h'].max() - nxt['l'].min(),
                     'minute': i})
r = pd.DataFrame(rows)
# the usual range at that time of day, so the open's big ranges don't do all the work
r['usual'] = r.groupby('minute')['rng_next'].transform('median')
r['next_rel'] = r['rng_next'] / r['usual']
r['past_rel'] = r['rng_past'] / r.groupby('minute')['rng_past'].transform('median')
print(f'n {len(r)} fifteen-minute steps over {mins.day.nunique()} days')
for col in ('g', 'absflow', 'vol', 'past_rel'):
    print(f'  corr({col:<8}, next range vs usual) = {np.corrcoef(r[col], r["next_rel"])[0, 1]:+.2f}')
# beyond the last 15 min's range: residual of next_rel on past_rel
beta = np.polyfit(r['past_rel'], r['next_rel'], 1)
res = r['next_rel'] - np.polyval(beta, r['past_rel'])
for col in ('g', 'absflow', 'vol'):
    print(f'  after the last range: corr({col:<8}, what is left) = {np.corrcoef(r[col], res)[0, 1]:+.2f}')
# split: customers bought gamma (dealers shorter) vs sold it
for name, sel in (('customers bought gamma (dealers got shorter)', r['g'] > 0),
                  ('customers sold gamma (dealers got longer)', r['g'] < 0)):
    print(f'  {name}: next range {r.loc[sel, "next_rel"].median():.2f}x usual (n {sel.sum()})')
