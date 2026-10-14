"""market_recorder: option ticks/minute bars, chain snapshots, ES book summary."""
import csv
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_recorder as mr  # noqa: E402

ET = ZoneInfo("America/New_York")


def at(y, mo, d, h, mi, s=0.0):
    return datetime(y, mo, d, h, mi, tzinfo=ET).timestamp() + s


def rows(tmp_path, f, day):
    f.flush(str(tmp_path))
    p = tmp_path / f"{f.prefix}_{day}.csv"
    return list(csv.DictReader(open(p, encoding="utf-8"))) if p.exists() else []


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "DATA_DIR", str(tmp_path))    # never the real data/ folder
    mr._opt_bar.clear(); mr._opt_prev.clear(); mr._es_state.clear()
    monkeypatch.setattr(mr, "_es_min", None)
    monkeypatch.setattr(mr, "_opt_minute", None)
    monkeypatch.setattr(mr, "_chain_far_at", 0.0)
    for f in mr.ALL_FILES:
        f._rows.clear()
    meta = {"A": ("2026-10-13", 7800.0, "call"), "B": ("2026-10-13", 7790.0, "put")}
    mr.configure(meta_fn=meta.get, greeks_fn=lambda s: (0.12, 0.5, 0.03), oi_fn=lambda s: 100,
                 spot_fn=lambda: 7801.25)
    yield


def test_calendar_and_hours():
    assert mr.third_friday(2026, 10) == date(2026, 10, 16)
    assert mr.next_monthly(date(2026, 10, 13)) == date(2026, 10, 16)
    assert mr.next_monthly(date(2026, 10, 17)) == date(2026, 11, 20)
    assert mr.next_monthly(date(2026, 12, 19)) == date(2027, 1, 15)
    assert mr.options_open(at(2026, 10, 13, 9, 30)) and not mr.options_open(at(2026, 10, 13, 16, 0))
    assert not mr.options_open(at(2026, 10, 10, 12, 0))                      # Saturday
    assert mr.es_open(at(2026, 10, 13, 3, 0)) and not mr.es_open(at(2026, 10, 13, 17, 30))
    assert not mr.es_open(at(2026, 10, 11, 17, 59)) and mr.es_open(at(2026, 10, 11, 18, 0))


def test_option_minute_bar_survives_an_early_next_minute_quote(tmp_path):
    m = at(2026, 10, 13, 10, 0)
    for s, (b, a) in ((1, (1.9, 2.1)), (10, (2.3, 2.5)), (20, (1.7, 1.9)), (50, (2.0, 2.2))):
        mr.option_quote("A", b, a, 2.05, 500 + s, ts=m + s)
    mr.option_quote("A", 4.9, 5.1, 5.0, 900, ts=m + 60.3)                    # beat the flusher
    mr.option_quote("B", 3.0, 3.2, 3.1, 40, ts=m + 5)
    mr.close_option_minute(int(m))
    r = {x["symbol"]: x for x in rows(tmp_path, mr.option_bars, "2026-10-13")}
    a = r["A"]
    assert (float(a["mid_open"]), float(a["mid_high"]), float(a["mid_low"]), float(a["mid_close"])) == (2.0, 2.4, 1.8, 2.1)
    assert a["bid"] == "2.0" and a["ask"] == "2.2" and a["volume"] == "550" and a["quotes"] == "4"
    assert a["expiry"] == "2026-10-13" and a["side"] == "call" and a["iv"] == "0.12" and a["spot"] == "7801.25"
    assert len(rows(tmp_path, mr.option_ticks, "2026-10-13")) == 6


def test_quiet_contract_repeats_its_last_close(tmp_path):
    m = at(2026, 10, 13, 10, 0)
    mr.option_quote("B", 3.0, 3.2, 3.1, 40, ts=m + 5)
    mr.close_option_minute(int(m))
    mr.close_option_minute(int(m + 60))                                      # no quote in this minute
    b = [x for x in rows(tmp_path, mr.option_bars, "2026-10-13") if x["symbol"] == "B"]
    assert [x["quotes"] for x in b] == ["1", "0"]
    assert b[1]["mid_open"] == b[1]["mid_close"] == "3.1" and b[1]["bid"] == "3.0"


def test_no_option_recording_outside_the_session(tmp_path):
    mr.option_quote("A", 1.0, 1.2, 1.1, 5, ts=at(2026, 10, 13, 16, 5))
    assert rows(tmp_path, mr.option_ticks, "2026-10-13") == []


def _chain():
    def opt(sym, bid):
        return [{"symbol": sym, "bid": bid, "ask": bid + 0.2, "last": bid, "mark": bid + 0.1,
                 "totalVolume": 10, "openInterest": 99, "volatility": 13.5, "delta": 0.5,
                 "gamma": 0.01, "theta": -1.0, "vega": 0.5}]
    exp = {"2026-10-26:0": "SPXW  261026C", "2026-10-27:1": "SPXW  261027C",
           "2026-11-13:18": "SPXW  261113C", "2026-11-20:25": "SPX   261120C"}
    calls = {k: {"7800.0": opt(f"{p}07800000", 5.0), "8100.0": opt(f"{p}08100000", 0.5)} for k, p in exp.items()}
    return {"callExpDateMap": calls, "putExpDateMap": {}}


def test_chain_cadence_band_and_skips(tmp_path):
    t0 = at(2026, 10, 26, 10, 0)                                             # Monday; monthly 11-20
    streamed = {"SPXW  261027C07800000"}
    assert mr.record_chain(_chain(), 7800.0, streamed, ts=t0) == 2           # 11-13 (far, due) + 11-20 monthly
    assert mr.record_chain(_chain(), 7800.0, set(), ts=t0 + 60) == 2         # 10-27 + monthly; far not due
    assert mr.record_chain(_chain(), 7800.0, set(), ts=t0 + 300) == 3        # far due again
    r = rows(tmp_path, mr.option_chain, "2026-10-26")
    assert {x["expiry"] for x in r} == {"2026-10-27", "2026-11-13", "2026-11-20"}   # never 0DTE
    assert all(x["strike"] == "7800.0" for x in r)                           # 8100 is outside +-200
    assert {x["root"] for x in r} == {"SPXW", "SPX"} and r[0]["iv"] == "13.5"


def test_es_minute_summary(tmp_path):
    m = at(2026, 10, 13, 10, 0)
    mr.es_quote({"bid": 7810.0, "ask": 7810.25, "bid_size": 30, "ask_size": 10, "volume": 1000, "last": 7810.0}, ts=m + 1)
    mr.es_quote({"bid_size": 10, "ask_size": 30}, ts=m + 10)
    mr.es_quote({"volume": 1005, "last": 7810.25, "last_size": 5}, ts=m + 20)
    mr.es_quote({"volume": 1030, "last": 7810.5, "last_size": 25}, ts=m + 30)
    mr.es_quote({"ask": 7810.75}, ts=m + 40)                                 # spread widens to 0.75
    mr.es_quote({"bid": 7811.0, "ask": 7811.25}, ts=m + 61)                  # next minute: closes the first
    r = rows(tmp_path, mr.es_book, "2026-10-13")
    assert len(r) == 1
    x = r[0]
    assert x["bid"] == "7810.0" and x["ask"] == "7810.75"                    # book at the close, not 7811
    assert x["quotes"] == "5" and x["trades"] == "2" and x["volume"] == "30"
    # time-weighted over the minute: spread 0.25 for 39 s then 0.75 for 20 s; imbalance
    # +0.5 for 9 s then -0.5 for 50 s (nothing known before the first update at +1 s)
    assert float(x["spread_max"]) == 0.75
    assert float(x["spread_avg"]) == round((0.25 * 39 + 0.75 * 20) / 59, 4)
    assert float(x["imbalance_avg"]) == round((0.5 * 9 - 0.5 * 50) / 59, 4)
    assert float(x["vwap"]) == round((7810.25 * 5 + 7810.5 * 25) / 30, 3)
    assert x["large_trades"] == "1" and x["large_volume"] == "25"
    assert (x["high"], x["low"], x["close"]) == ("7810.5", "7810.0", "7810.5")
    mr.tick(now=m + 125)              # the second minute closes on the clock (no update after +61 s)
    r2 = rows(tmp_path, mr.es_book, "2026-10-13")[-1]
    assert r2["time"] == str(int(m + 60)) and r2["quotes"] == "1"
    assert float(r2["spread_avg"]) == round((0.75 * 1 + 0.25 * 59) / 60, 4)   # 0.75 carried 1 s
    assert len(rows(tmp_path, mr.es_ticks, "2026-10-13")) == 6


def test_day_file_header_written_once(tmp_path):
    f = mr._DayCsv("x_test", ["a", "b"])
    f.add("2026-10-13", [1, 2]); f.flush(str(tmp_path))
    f.add("2026-10-13", [3, 4]); f.flush(str(tmp_path))
    assert (tmp_path / "x_test_2026-10-13.csv").read_text().splitlines() == ["a,b", "1,2", "3,4"]


def test_internals_minute_bars_and_carry(tmp_path, monkeypatch):
    mr._int_bar.clear()
    monkeypatch.setattr(mr, "_int_minute", None)
    m = at(2026, 10, 13, 10, 0)
    for s, v in ((1, -200.0), (20, 650.0), (40, -900.0), (55, -120.0)):
        mr.internal_quote("$TICK", v, None, ts=m + s)
    mr.internal_quote("SPY", 777.9, 13_000_000, ts=m + 5)
    mr.internal_quote("$TICK", 300.0, None, ts=m + 60.4)               # beats the flusher
    mr.close_internal_minute(int(m))
    mr.close_internal_minute(int(m + 60))
    r = rows(tmp_path, mr.internals, "2026-10-13")
    tick0 = [x for x in r if x["symbol"] == "$TICK" and x["time"] == str(int(m))][0]
    assert (tick0["open"], tick0["high"], tick0["low"], tick0["close"], tick0["updates"]) == ("-200.0", "650.0", "-900.0", "-120.0", "4")
    spy = [x for x in r if x["symbol"] == "SPY"]
    assert [x["updates"] for x in spy] == ["1", "0"] and spy[1]["close"] == "777.9" and spy[1]["volume"] == "13000000"
    assert [x["time"] for x in r if x["symbol"] == "$TICK"] == [str(int(m)), str(int(m + 60))]


def test_event_calendar():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import build_event_calendar as cal
    rows = cal.build()
    ev = {(r[0], r[3]) for r in rows}
    assert ("2026-06-18", "opex") in ev and ("2026-06-19", "opex") not in ev   # Juneteenth: Thursday
    assert ("2026-10-21", "vix_exp") in ev and ("2026-10-14", "cpi") in ev and ("2026-10-28", "fomc") in ev
    assert ("2026-09-30", "period_end") in ev                                  # quarter end
    market_events = [r for r in rows if r[3] in ("opex", "vix_exp", "period_end", "fomc", "early_close")]
    assert all(cal.trading_day(date.fromisoformat(r[0])) for r in market_events)
    gf = [r for r in rows if r[0] == "2026-04-03" and r[3] == "jobs"][0]       # released on Good Friday
    assert "stock market closed" in gf[2]
    assert len(rows) == len({tuple(r) for r in rows})                          # no duplicates


def test_es_unchanged_price_trade_sets_high_low_and_session_reset_counts(tmp_path):
    m = at(2026, 10, 13, 18, 0)                         # the first minute after the daily halt
    mr.es_quote({"bid": 7855.0, "ask": 7855.25, "last": 7855.25, "volume": 1_900_000}, ts=m - 3700)  # 16:58
    mr.es_quote({"volume": 120}, ts=m + 2)               # new session: day volume restarted
    mr.es_quote({"volume": 150, "last": 7855.0}, ts=m + 10)
    mr.tick(now=m + 61)
    r = [x for x in rows(tmp_path, mr.es_book, "2026-10-13") if x["time"] == str(int(m))][0]
    assert r["volume"] == "150" and r["trades"] == "2"   # 120 since the restart + 30
    assert (r["high"], r["low"]) == ("7855.25", "7855.0") # the volume-only trade at 7855.25 counts
    assert float(r["low"]) <= float(r["vwap"]) <= float(r["high"])


def test_stream_routes_internals_away_from_price_paths():
    import streamer as st
    got, book, ticks = [], [], []
    s = st.SchwabStreamer(on_candle=lambda c: None, on_futures_quote=book.append,
                          on_internal_quote=lambda sym, last, vol: got.append((sym, last, vol)))
    s._merge_tick = lambda t: t
    s._handle_tick = lambda t: ticks.append(t["symbol"])
    s._handle_data({"data": [
        {"service": "LEVELONE_EQUITIES", "content": [{"key": "$SPX", "3": 7800.5},
                                                     {"key": "$DVOL", "3": 1.0, "8": "92233720368.54776"},
                                                     {"key": "NVDA", "3": 229.05, "8": 1000}]},
        {"service": "LEVELONE_FUTURES", "content": [{"key": "/ES", "1": 7855.0, "3": 7855.25, "8": 10},
                                                    {"key": "/NQ", "3": 30769.75, "8": 55}]}]})
    assert got == [("$DVOL", 1.0, 92233720368), ("NVDA", 229.05, 1000), ("/NQ", 30769.75, 55)]
    assert ticks == ["$SPX", "/ES"]                       # only these build candles
    assert [q["symbol"] for q in book] == ["/ES"]         # and only ES feeds the order-book recorder
