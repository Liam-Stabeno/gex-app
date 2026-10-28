import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delta_flow as D  # noqa: E402

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 21, 10, 0, tzinfo=ET).timestamp()     # a Wednesday, in session


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "_state", {"day": None, "cum": dict.fromkeys(D.SERIES, 0.0),
                                      "points": {k: [] for k in D.SERIES}})
    monkeypatch.setattr(D, "_last", {})
    monkeypatch.setattr(D, "_saved_at", 0.0)
    monkeypatch.setattr(D, "_pushed_at", 0.0)
    monkeypatch.setattr(D, "_sse_push", None)
    monkeypatch.setattr(D, "_DATA_DIR", str(tmp_path))
    return tmp_path


def test_classify():
    assert D.classify(1.0, 1.2, None, 1.2) == 1          # at the ask
    assert D.classify(1.0, 1.2, None, 1.25) == 1         # through it, within a spread
    assert D.classify(1.0, 1.2, None, 1.0) == -1         # at the bid
    assert D.classify(1.0, 1.2, None, 1.15) == 1         # above the mid
    assert D.classify(1.0, 1.2, None, 1.05) == -1        # below it
    assert D.classify(1.0, 1.2, 1.0, 1.1) == 1           # at the mid, up from the last trade
    assert D.classify(1.0, 1.2, 1.2, 1.1) == -1          # down from it
    assert D.classify(1.0, 1.2, 1.1, 1.1) == 0           # no change: unknown
    assert D.classify(1.0, 1.2, None, 1.5) is None       # more than a spread outside: a leg of a package
    assert D.classify(1.0, 1.2, None, 0.7) is None
    assert D.classify(0.0, 0.0, None, 1.0) is None       # no quote
    assert D.classify(1.0, 1.2, None, 0.0) is None       # no trade price


def tick(sym, bid, ask, last, vol, t, side="call", odte=True, dl=0.5, spot=7700.0):
    D.record_tick(sym, bid, ask, last, vol, lambda: (side, odte, dl, spot), ts=t)


def test_signs_and_series():
    # call bought at the ask: +; put bought at the ask (delta < 0): -; call sold at the bid: -
    tick("C", 1.0, 1.2, 1.1, 100, T0)                   # first tick: baseline only
    tick("C", 1.0, 1.2, 1.2, 110, T0 + 1)                # 10 bought
    tick("P", 2.0, 2.2, 2.1, 50, T0 + 2, side="put", dl=-0.4)
    tick("P", 2.0, 2.2, 2.2, 55, T0 + 3, side="put", dl=-0.4)      # 5 bought
    tick("N", 3.0, 3.2, 3.1, 7, T0 + 4, odte=False)
    tick("N", 3.0, 3.2, 3.0, 9, T0 + 5, odte=False)                # 2 sold, next expiration
    c = D._state["cum"]
    assert c["call"] == pytest.approx(10 * 0.5 * 100 * 7700 - 2 * 0.5 * 100 * 7700)
    assert c["0dte_call"] == pytest.approx(10 * 0.5 * 100 * 7700), "the next expiration isn't 0DTE"
    assert c["put"] == pytest.approx(-5 * 0.4 * 100 * 7700) == c["0dte_put"]
    assert D.get_series(datetime.fromtimestamp(T0, ET))["call"] == [{"time": int(T0 // 60) * 60, "value": c["call"]}]


def test_quote_only_and_resets_dont_count():
    tick("C", 1.0, 1.2, 1.1, 100, T0)
    tick("C", 1.1, 1.3, 1.1, 100, T0 + 1)               # quote moved, no trade
    tick("C", 1.1, 1.3, 1.1, 40, T0 + 2)                # volume fell (a new day's count)
    assert D._state["day"] is None
    tick("C", 1.1, 1.3, 1.3, 45, T0 + 3)                # 5 bought against the quote before
    assert D._state["cum"]["call"] == pytest.approx(5 * 0.5 * 100 * 7700)


def test_package_legs_and_out_of_session_skipped():
    tick("C", 1.0, 1.2, 1.1, 100, T0)
    tick("C", 1.0, 1.2, 2.0, 200, T0 + 1)               # far above the ask: a multi-leg print
    pre = datetime(2026, 10, 21, 9, 29, tzinfo=ET).timestamp()
    tick("D", 1.0, 1.2, 1.1, 1, pre - 1)
    tick("D", 1.0, 1.2, 1.2, 9, pre)                    # before the open
    assert D._state["day"] is None


def test_minutes_save_restore_and_new_day(fresh):
    tick("C", 1.0, 1.2, 1.1, 0, T0)
    tick("C", 1.0, 1.2, 1.2, 1, T0 + 1)
    tick("C", 1.0, 1.2, 1.2, 3, T0 + 61)                # the next minute
    pts = D._state["points"]["call"]
    assert [p["time"] for p in pts] == [int(T0 // 60) * 60, int(T0 // 60) * 60 + 60]
    saved = json.loads((fresh / "delta_flow_ticks_2026-10-21.json").read_text())
    assert saved["cum"]["call"] == pytest.approx(3 * 0.5 * 100 * 7700)
    D._state = {"day": None, "cum": dict.fromkeys(D.SERIES, 0.0), "points": {k: [] for k in D.SERIES}}
    D.load_today(datetime.fromtimestamp(T0 + 120, ET))
    assert D._state["cum"]["call"] == pytest.approx(3 * 0.5 * 100 * 7700) and len(D._state["points"]["call"]) == 2
    # the next day starts from zero, and yesterday's series isn't served as today's
    assert D.get_series(datetime.fromtimestamp(T0 + 86400, ET)) == {"call": [], "put": []}
    tick("C", 1.0, 1.2, 1.2, 5, T0 + 86400)
    assert D._state["day"] == "2026-10-22" and D._state["cum"]["call"] == pytest.approx(2 * 0.5 * 100 * 7700)


def test_push_throttled(monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_sse_push", sent.append)
    tick("C", 1.0, 1.2, 1.1, 0, T0)
    for i in range(1, 6):
        tick("C", 1.0, 1.2, 1.2, i, T0 + i * 0.2)        # five trades within a second
    assert len(sent) == 1 and sent[0]["type"] == "delta_flow" and set(D.SERIES) <= set(sent[0])
