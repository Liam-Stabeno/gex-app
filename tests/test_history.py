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


def test_save_bars_keeps_rows_of_an_already_gzipped_day(tmp_path):
    import gzip as gz
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import gex_stats
    from datetime import date
    row = lambda t, s: [t, s, "2026-09-01", 7800.0, "call", 1.0, 1.0, 1.0, 1.0, 5]
    oh.save_bars({"2026-09-01": [row(100, "A"), row(200, "A")]}, str(tmp_path))
    gex_stats.archive_old_files("SPX", today=date(2026, 10, 1), data_dir=str(tmp_path))   # gzipped, plain gone
    assert (tmp_path / "option_trade_bars_SPX_2026-09-01.csv.gz").exists()
    assert oh.save_bars({"2026-09-01": [row(200, "A"), row(300, "B")]}, str(tmp_path)) == 1   # 200/A already there
    gex_stats.archive_old_files("SPX", today=date(2026, 10, 1), data_dir=str(tmp_path))   # re-gzip the superset
    with gz.open(tmp_path / "option_trade_bars_SPX_2026-09-01.csv.gz", "rt") as f:
        lines = f.read().splitlines()
    assert [l.split(",")[:2] for l in lines[1:]] == [["100", "A"], ["200", "A"], ["300", "B"]]


def test_lock_held_by_a_dead_process_is_taken_over(tmp_path):
    p = str(tmp_path / "x.lock")
    open(p, "w").write("999999")                    # no such process (a killed app copy)
    with oh.history_lock(wait=0, path=p) as l:
        assert l.ok and open(p).read() == str(os.getpid())
    open(p, "w").write(str(os.getpid()))            # a live holder (this process) is respected
    with oh.history_lock(wait=0, path=p) as l2:
        assert not l2.ok
    os.remove(p)


def test_daily_request_params_and_failed_requests_not_marked_done(monkeypatch, tmp_path):
    sent, codes = [], iter([500, 200, 200, 500])       # A fails, and fails its retry too
    class R:
        def __init__(self, code): self.status_code, self.ok, self.text = code, code == 200, "err"
        def json(self): return {"candles": [{"datetime": 1_791_900_000_000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]}
    monkeypatch.setattr(oh.requests, "get", lambda url, **kw: sent.append(kw["params"]) or R(next(codes)))
    monkeypatch.setattr(oh, "get_access_token", lambda force=False: "t")
    monkeypatch.setattr(oh.time, "sleep", lambda s: None)
    done = []
    c = [("A", "2026-12-18", 7800.0, "call"), ("B", "2026-12-18", 7800.0, "put"), ("C", "2026-12-18", 7805.0, "call")]
    s = oh.pull(c, 0, 1, rate=0, size="1d", data_dir=str(tmp_path), on_saved=done.extend)
    assert sent[0]["periodType"] == "year" and sent[0]["frequencyType"] == "daily"
    assert s["errors"] == 1 and done == ["B", "C"]          # A failed: retried next run


def test_all_listed_contracts_every_expiration(monkeypatch):
    class R:
        def __init__(self, data, ok=True): self.data, self.ok = data, ok
        def json(self): return self.data
    def get(url, **kw):
        if url.endswith("expirationchain"):
            return R({"expirationList": [{"expirationDate": "2026-10-13", "expirationType": "W"},
                                         {"expirationDate": "2026-12-18", "expirationType": "S"}]})
        if kw["params"]["fromDate"] == "2026-10-13":
            return R({}, ok=False)                                   # an expiration that ended today
        o = lambda s, v: [{"symbol": s, "totalVolume": v}]
        return R({"underlyingPrice": 7800.0, "callExpDateMap": {"2026-12-18:66": {
            "7800.0": o("D7800", 3), "8700.0": o("D8700", 9), "7100.0": o("D7100", 0)}}, "putExpDateMap": {}})
    monkeypatch.setattr(oh.requests, "get", get)
    monkeypatch.setattr(oh, "get_access_token", lambda force=False: "t")
    c, spot = oh.all_listed_contracts(10.0, pause=0)
    assert spot == 7800.0 and [(x[0], x[4]) for x in c] == [("D7800", "S"), ("D7100", "S")]   # 8700: beyond 10% (780 pts)
    c2, _ = oh.all_listed_contracts(10.0, traded_only=True, pause=0)
    assert [x[0] for x in c2] == ["D7800"]                                                    # D7100 didn't trade


def test_failed_request_is_retried_once_at_the_end(monkeypatch, tmp_path):
    codes = iter([503, 200, 200])                     # A fails, B ok, then A's retry ok
    class R:
        def __init__(self, c): self.status_code, self.ok, self.text = c, c == 200, ""
        def json(self): return {"candles": [{"datetime": 1_791_900_000_000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]}
    monkeypatch.setattr(oh.requests, "get", lambda url, **kw: R(next(codes)))
    monkeypatch.setattr(oh, "get_access_token", lambda force=False: "t")
    monkeypatch.setattr(oh.time, "sleep", lambda s: None)
    done = []
    s = oh.pull([("A", "2026-12-18", 7800.0, "call"), ("B", "2026-12-18", 7800.0, "put")], 0, 1, rate=0,
                data_dir=str(tmp_path), on_saved=done.extend)
    assert s["errors"] == 0 and sorted(done) == ["A", "B"] and s["with_bars"] == 2
