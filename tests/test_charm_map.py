"""
Charm map (src/charm_map.py): dealer charm flow by price, its PULL and SPLIT levels.

Run from the project root:   python -m pytest tests/test_charm_map.py -q
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import bs  # noqa: E402
import charm_map as cm  # noqa: E402

ET = ZoneInfo('America/New_York')
HR = 1 / (365 * 24)


def test_vectorised_flow_matches_the_scalar_formulas():
    contracts = [(7690.0, 'put', 5000, 0.14, HR), (7720.0, 'call', 3000, 0.12, 2 * HR), (7700.0, 'put', 800, 0.2, 30 * HR)]
    want = sum(bs.dealer_charm_flow(bs._bs_charm(7705.0, k, t, cm.R, v), oi, 7705.0, side)
               for k, side, oi, v, t in contracts) / (365 * 24)
    assert cm.flow_at(contracts, 7705.0) == pytest.approx(want, rel=1e-9)


def test_dealers_long_calls_pull_short_puts_split():
    # long a call block: BUY below it, SELL above it: decay pins price to it
    m = cm.charm_map([(7740.0, 'call', 10000, 0.15, HR)], 7730.0)
    assert m['pull'] and abs(m['pull'][0] - 7740) < 3 and not m['split']
    # short a put block: SELL below, BUY above: decay pushes price off it
    m = cm.charm_map([(7700.0, 'put', 10000, 0.15, HR)], 7705.0)
    assert m['split'] and abs(m['split'][0] - 7700) < 3 and not m['pull']


def test_puts_below_and_calls_above_drift_price_up_to_the_calls():
    # the 2026-10-29 close: put mass below price, decaying; the flow between is BUY
    m = cm.charm_map([(7690.0, 'put', 10000, 0.15, HR), (7740.0, 'call', 6000, 0.15, HR)], 7712.0)
    assert cm.flow_at([(7690.0, 'put', 10000, 0.15, HR), (7740.0, 'call', 6000, 0.15, HR)], 7712.0) > 0
    assert abs(m['pull'][0] - 7740) < 4
    assert len(m['spots']) == 81 and m['spots'][40] == 7712.0


def test_watchlist_contracts_recover_the_vol_from_delta():
    ts = datetime(2026, 10, 29, 15, 0, tzinfo=ET)
    T = 3600 / (365 * 24 * 3600)
    rows = []
    for k, sig in ((7690.0, 0.14), (7720.0, 0.11)):
        for side in ('call', 'put'):
            rows.append({'symbol': f"SPXW  261029{side[0].upper()}0{int(k)}000", 'strike': k, 'side': side,
                         'delta': round(bs.delta(7705.0, k, T, cm.R, sig, side[0]), 6), 'oi': 100})
    rows.append({'symbol': 'SPXW  261030C07720000', 'strike': 7720.0, 'side': 'call', 'delta': 0.3, 'oi': 50})  # not 0DTE
    got = {(k, side): v for k, side, oi, v, t in cm.watchlist_contracts(rows, 7705.0, ts)}
    assert len(got) == 4
    assert got[(7690.0, 'put')] == pytest.approx(0.14, abs=0.002) and got[(7720.0, 'call')] == pytest.approx(0.11, abs=0.002)
    assert len(cm.watchlist_contracts(rows, 7705.0, ts, zero_dte_only=False)) == 5
