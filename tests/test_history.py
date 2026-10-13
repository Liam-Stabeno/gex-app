"""option_history / market_history: contract selection, per-day merge, lock, finished bars."""
import csv
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["GEX_NO_AUTO_REAUTH"] = "1"
import option_history as oh  # noqa: E402


def _chain():
    o = lambda sym, v: [{"symbol": sym, "totalVolume": v}]
    return {"callExpDateMap": {"2026-10-13:0": {"7800.0": o("C0_7800", 5), "8200.0": o("C0_8200", 1)},
                               "2026-10-14:1": {"7805.0": o("C1_7805", 0), "7790.0": o("C1_7790", 3)}},
            "putExpDateMap": {"2026-10-13:0": {"7795.0": o("P0_7795", 0)}}}


def test_contracts_from_chain():
    c = oh.contracts_from_chain(_chain(), 7800.0, 300)
    assert [x[0] for x in c] == ["C0_7800", "P0_7795", "C1_7805", "C1_7790"]   # nearest expiry, then distance
    assert oh.contracts_from_chain(_chain(), 7800.0, 300, expiries={"2026-10-14"})[0][1] == "2026-10-14"
    assert [x[0] for x in oh.contracts_from_chain(_chain(), 7800.0, 300, traded_only=True)] == ["C0_7800", "C1_7790"]


def test_save_bars_merges_per_day_without_duplicates(tmp_path):
    row = lambda t, s, c: [t, s, "2026-10-13", 7800.0, "call", c, c, c, c, 10]
    assert oh.save_bars({"2026-10-13": [row(200, "A", 1.0), row(100, "A", 2.0)]}, str(tmp_path)) == 2
    assert oh.save_bars({"2026-10-13": [row(100, "A", 9.9), row(300, "B", 3.0)]}, str(tmp_path)) == 1
    r = list(csv.DictReader(open(tmp_path / "option_trade_bars_SPX_2026-10-13.csv")))
    assert [(x["time"], x["symbol"], x["close"]) for x in r] == [("100", "A", "2.0"), ("200", "A", "1.0"), ("300", "B", "3.0")]


def test_history_lock(tmp_path):
    p = str(tmp_path / "x.lock")
    with oh.history_lock(wait=0, path=p) as a:
        assert a.ok and os.path.exists(p)
        with oh.history_lock(wait=0, path=p) as b:
            assert not b.ok                                   # second writer waits / gives up
    assert not os.path.exists(p)
    open(p, "w").write("1")                                  # a crashed run's lock...
    os.utime(p, (time.time() - oh.LOCK_STALE_SEC - 5,) * 2)
    with oh.history_lock(wait=0, path=p) as c:
        assert c.ok                                           # ...is taken over


def test_market_history_saves_only_finished_bars(monkeypatch):
    import market_history as mh
    saved = []
    monkeypatch.setattr(mh.ph, "append_new", lambda sym, c, interval: saved.extend(c) or c)
    now_ms = int(time.time() * 1000)
    bars = [{"datetime": now_ms - 600_000}, {"datetime": now_ms - 120_000}]     # the second: 5-min bar in progress
    assert mh._save("$VIX", bars, "5m", 300_000) == 1 and saved == bars[:1]
