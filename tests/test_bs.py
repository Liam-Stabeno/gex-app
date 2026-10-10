"""
Run from the project root:   python -m pytest tests/test_bs.py -q
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import bs  # noqa: E402

ET = ZoneInfo("America/New_York")
S, R = 7830.0, 0.05
T_3H = 3.0 / (365 * 24)          # 0DTE, three hours to the close


@pytest.mark.parametrize("K", [7600, 7700, 7780, 7810, 7830, 7855, 7900, 8000])
@pytest.mark.parametrize("flag", ["c", "p"])
@pytest.mark.parametrize("vol", [0.08, 0.15, 0.40])
def test_iv_recovers_vol_across_strikes(K, flag, vol):
    price = bs._bs_price(S, K, T_3H, R, vol, flag)
    if price < 0.01:              # below a tick: not quotable, skip
        pytest.skip("sub-tick price")
    # deep ITM: a ±5% vol move changes the price by under a cent, so the price
    # carries no vol information (the live loop inverts the OTM side instead)
    if abs(bs._bs_price(S, K, T_3H, R, vol * 1.05, flag) - bs._bs_price(S, K, T_3H, R, vol * 0.95, flag)) < 0.01:
        pytest.skip("price insensitive to vol")
    assert bs.iv(price, S, K, T_3H, R, flag) == pytest.approx(vol, rel=1e-3)


def test_cheap_otm_0dte_regression():
    """7855 call at 0.475 with ~3h left: the old Newton solver returned 0.8%."""
    v = bs.iv(0.475, 7828.94, 7855.0, T_3H, R, "c")
    assert v is not None and 0.05 < v < 0.30
    assert bs.greeks(7828.94, 7855.0, T_3H, R, v)[0] > 0.001


def test_iv_rejects_impossible_prices():
    assert bs.iv(0.0, S, 7900, T_3H, R, "c") is None
    assert bs.iv(10.0, S, 7700, T_3H, R, "c") is None          # below intrinsic (130)
    assert bs.iv(5000.0, S, 7900, T_3H, R, "c") is None        # above any vol
    assert bs.iv(1.0, S, 7900, 0.0, R, "c") is None


def test_call_and_put_share_gamma():
    v = 0.12
    assert bs.greeks(S, 7850, T_3H, R, v)[0] == pytest.approx(bs._bs_gamma(S, 7850, T_3H, R, v))


def test_t_to_close_decays_intraday():
    at = lambda h, m: datetime(2026, 10, 6, h, m, tzinfo=ET)
    t_open  = bs.t_to_close("2026-10-06", at(9, 30))
    t_noon  = bs.t_to_close("2026-10-06", at(13, 0))
    t_close = bs.t_to_close("2026-10-06", at(15, 59))
    assert t_open == pytest.approx(6.5 / (365 * 24))
    assert t_noon == pytest.approx(3.0 / (365 * 24))
    assert t_close == pytest.approx(300 / (365 * 24 * 3600))   # 5-minute floor
    assert bs.t_to_close("2026-10-07", at(13, 0)) == pytest.approx(27.0 / (365 * 24))
    assert bs.t_to_close("2026-10-05", at(13, 0)) == 0.0        # already expired
    assert bs.t_to_close("2026-10-06", at(16, 0)) == 0.0         # expired at the close:
    assert bs.t_to_close("2026-10-06", at(16, 13)) == 0.0        # no 5-minute floor after it


def test_drop_expired_removes_todays_expiry_after_the_close():
    from gex import drop_expired
    chain = {"underlyingPrice": 7765.0,
             "callExpDateMap": {"2026-10-08:0": {"7765.0": [{}]}, "2026-10-09:1": {"7765.0": [{}]}},
             "putExpDateMap": {"2026-10-08:0": {"7765.0": [{}]}, "2026-10-09:1": {"7765.0": [{}]}}}
    at = lambda h, m: datetime(2026, 10, 8, h, m, tzinfo=ET)
    assert drop_expired(chain, at(15, 59)) is chain                    # in session: untouched
    after = drop_expired(chain, at(16, 0))
    assert list(after["callExpDateMap"]) == ["2026-10-09:1"] == list(after["putExpDateMap"])
    assert after["underlyingPrice"] == 7765.0 and len(chain["callExpDateMap"]) == 2   # input kept


def test_true_pin_needs_to_lead_for_the_confirm_time():
    from gex import confirmed_pick
    p = {}
    assert confirmed_pick(7750.0, None, p, 0) == 7750.0               # first pin shows at once
    assert confirmed_pick(7720.0, 7750.0, p, 10) == 7750.0            # challenger starts waiting
    assert confirmed_pick(7750.0, 7750.0, p, 60) == 7750.0 and p == {}  # it fell back: reset
    assert confirmed_pick(7720.0, 7750.0, p, 70) == 7750.0
    assert confirmed_pick(7700.0, 7750.0, p, 100) == 7750.0           # another strike: restarts
    assert confirmed_pick(7700.0, 7750.0, p, 279) == 7750.0
    assert confirmed_pick(7700.0, 7750.0, p, 280) == 7700.0           # led 180 s: moves
    assert confirmed_pick(None, 7700.0, p, 300) == 7700.0


def test_true_pin_hysteresis():
    from gex import pick_with_hysteresis
    s = {7810.0: 1.00, 7845.0: 1.05}
    assert pick_with_hysteresis(s, None) == 7845.0                 # no current: plain argmax
    assert pick_with_hysteresis(s, 7810.0) == 7810.0               # +5% is not enough to move
    assert pick_with_hysteresis({7810.0: 1.0, 7845.0: 1.2}, 7810.0) == 7845.0   # +20% moves it
    assert pick_with_hysteresis(s, 7700.0) == 7845.0               # current strike gone
    assert pick_with_hysteresis({}, 7810.0) is None


def test_stream_candles_only_in_trading_hours():
    from background import in_trading_hours
    ms = lambda d, h, m: int(datetime(2026, 10, d, h, m, tzinfo=ET).timestamp() * 1000)   # Oct 2026: 9 Fri, 10 Sat, 11 Sun
    assert in_trading_hours('$SPX', ms(9, 9, 30)) and in_trading_hours('$SPX', ms(9, 15, 59))
    assert not in_trading_hours('$SPX', ms(9, 9, 14))          # pre-market snapshot tick
    assert not in_trading_hours('$SPX', ms(9, 16, 0))
    assert not in_trading_hours('$SPX', ms(10, 12, 0))         # Saturday
    assert in_trading_hours('/ES', ms(9, 16, 59)) and not in_trading_hours('/ES', ms(9, 17, 0))   # Friday close
    assert not in_trading_hours('/ES', ms(10, 15, 2))          # Saturday
    assert not in_trading_hours('/ES', ms(11, 17, 59)) and in_trading_hours('/ES', ms(11, 18, 0))  # Sunday open
    assert not in_trading_hours('/ES', ms(8, 17, 30)) and in_trading_hours('/ES', ms(8, 3, 0))     # daily halt / overnight
