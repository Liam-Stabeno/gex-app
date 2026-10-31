"""
tools/signed_gex_test.py: the dealers' position from each reading, and no look-ahead.

Run from the project root:   python -m pytest tests/test_signed_gex.py -q
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import charm_map as cm  # noqa: E402
import signed_gex_test as sg  # noqa: E402


def trades(rows):
    return pd.DataFrame(rows, columns=['minute', 'symbol', 's', 'dv', 'outside'])


def test_net_bought_adds_up_and_skips_what_cant_be_classed():
    f = sg.net_bought(trades([(60, 'A', 1, 10, False), (60, 'A', -1, 4, False), (120, 'A', 1, 1, False),
                              (120, 'B', -1, 5, False), (120, 'A', 0, 99, False), (120, 'A', 1, 99, True)]))
    assert f.loc[60, 'A'] == 6 and f.loc[60, 'B'] == 0
    assert f.loc[120, 'A'] == 7 and f.loc[120, 'B'] == -5


def test_latest_only_uses_minutes_already_finished():
    f = pd.DataFrame({'A': [1.0, 2.0]}, index=[60, 120])
    assert sg.latest(f, 119) is None or sg.latest(f, 119)['A'] == 1.0
    assert sg.latest(f, 60) is None                     # the 60 minute is still running
    assert sg.latest(f, 120)['A'] == 1.0
    assert sg.latest(f, 179)['A'] == 1.0
    assert sg.latest(f, 180)['A'] == 2.0


def test_customers_buying_takes_gamma_from_dealers_call_or_put():
    # 10 bought, gamma 0.02, SPX 7000: the heatmap's gamma x contracts x 100 x spot, negative
    assert sg.dealer_gex_change([10], [0.02], 7000)[0] == pytest.approx(-140000)
    assert sg.dealer_gex_change([-10], [0.02], 7000)[0] == pytest.approx(140000)


def test_positions_start_from_long_calls_short_puts_and_buying_lowers_both():
    p = sg.positions([1, -1], [100, 100], np.array([30.0, 30.0]))
    assert list(p['oi']) == [100, -100]
    assert list(p['signed']) == [70, -130]
    assert list(p['flow']) == [-30, -30]


def test_charm_flow_from_oi_is_the_charm_maps_own():
    contracts = [(7000.0, 'call', 500, 0.2, 0.001), (6950.0, 'put', 800, 0.25, 0.001), (7050.0, 'call', 300, 0.2, 0.001)]
    k = np.array([c[0] for c in contracts])
    sign = np.array([1.0 if c[1] == 'call' else -1.0 for c in contracts])
    iv = np.array([c[3] for c in contracts])
    pos = sg.positions(sign, [c[2] for c in contracts], np.zeros(3))['oi']
    assert sg.charm_flow(k, pos, iv, 0.001, 7010.0) == pytest.approx(cm.flow_at(contracts, 7010.0))


def test_a_short_call_flips_the_charm_flow():
    k, iv = np.array([7000.0]), np.array([0.2])
    long_ = sg.charm_flow(k, np.array([100.0]), iv, 0.001, 7010.0)
    assert sg.charm_flow(k, np.array([-100.0]), iv, 0.001, 7010.0) == pytest.approx(-long_)


def test_variant_columns_adjusts_the_right_strike_with_the_finished_minute():
    heat = {'times': [150], 'spots': [7000.0], 'strikes': [6995.0, 7000.0], 'all': [[100.0, 200.0]]}
    flows = pd.DataFrame({'A': [10.0], 'FAR': [50.0]}, index=[60])
    book = {'gamma': pd.DataFrame({'A': [0.01], 'FAR': [0.01]}, index=[60]),
            'meta': pd.DataFrame({'strike': [7000.0, 8000.0]}, index=['A', 'FAR'])}
    c = sg.variant_columns(heat, flows, book)
    change = sg.dealer_gex_change([10.0], [0.01], 7000.0)[0]
    assert c['oi'] == heat['all']
    assert c['flow'][0] == [0.0, change]                # 8000 isn't on the heatmap
    assert c['signed'][0] == [100.0, 200.0 + change]


def test_no_finished_minute_leaves_oi_alone():
    heat = {'times': [90], 'spots': [7000.0], 'strikes': [7000.0], 'all': [[100.0]]}
    flows = pd.DataFrame({'A': [10.0]}, index=[60])
    book = {'gamma': pd.DataFrame({'A': [0.01]}, index=[60]), 'meta': pd.DataFrame({'strike': [7000.0]}, index=['A'])}
    assert sg.variant_columns(heat, flows, book)['signed'] == [[100.0]]


def test_balanced_accuracy_gives_always_saying_bigger_no_credit():
    # bigger 80% of the time: "always bigger" hits 80%, but it's a coin on balanced accuracy
    x = pd.DataFrame({'bigger': [True] * 8 + [False] * 2, 'hit_v': [True] * 8 + [False] * 2})
    assert x['hit_v'].mean() == 0.8
    assert sg.balanced(x, 'v') == 0.5


def test_balanced_accuracy_needs_both_kinds_of_move():
    assert np.isnan(sg.balanced(pd.DataFrame({'bigger': [True, True], 'hit_v': [True, False]}), 'v'))


def test_holds_needs_the_bar_and_both_halves():
    assert sg.holds((5.0, 2.0, 4.0, 6.0))
    assert not sg.holds((5.0, 8.0, 4.0, 6.0))           # inside its bar
    assert not sg.holds((5.0, 2.0, 12.0, -2.0))         # one half alone
