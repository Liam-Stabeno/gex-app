"""
Run from the project root:   python -m pytest tests/test_key_dates.py -q
"""
import gzip
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import bs              # noqa: E402
import key_dates as kd  # noqa: E402

ET = kd.ET
CSV = """date,time_et,event,category,importance,source
2026-10-28,14:00,FOMC decision,fomc,high,fed
2026-10-30,,Month end,period_end,low,computed
2026-11-06,08:30,Jobs report (Employment Situation),jobs,high,bls
2026-11-18,09:30,VIX expiration,vix_exp,medium,computed
2026-11-20,,Monthly option expiration,opex,medium,computed
2026-11-26,,Market closed: Thanksgiving,holiday,high,nyse
2026-12-18,,Quarterly expiration (quad witching),opex,high,computed
2026-12-31,,Quarter end,period_end,medium,computed
2026-12-31,08:30,CPI,cpi,high,bls
"""


@pytest.fixture
def cal(tmp_path, monkeypatch):
    p = tmp_path / "market_events.csv"
    p.write_text(CSV, encoding="utf-8")
    monkeypatch.setattr(kd, "_events", {"sig": None, "red": {}, "holidays": set()})
    monkeypatch.setattr(kd, "_snaps", {})
    monkeypatch.setattr(kd, "_sides", {})
    monkeypatch.setattr(kd, "_ohlc", {})
    monkeypatch.setattr(kd, "_notes_saved", {"sig": None, "rows": []})
    return str(p)


def test_red_days_tags(cal):
    red = kd.red_days(cal)
    assert red["2026-10-28"] == ["FOMC"] and red["2026-10-30"] == ["MONTH END"]
    assert red["2026-11-06"] == ["JOBS REPORT"] and red["2026-11-20"] == ["MONTHLY OPEX"]
    assert red["2026-12-18"] == ["QUARTERLY OPEX"]
    assert red["2026-12-31"] == ["QUARTER END", "CPI"]
    assert "2026-11-18" not in red and "2026-11-26" not in red, "VIX expiry and holidays aren't red"


def test_next_red_days_and_sessions_away(cal):
    before = datetime(2026, 10, 28, 15, 0, tzinfo=ET)
    after = datetime(2026, 10, 28, 16, 5, tzinfo=ET)
    assert [d for d, _ in kd.next_red_days(2, before, cal)] == ["2026-10-28", "2026-10-30"], "today until the close"
    assert [d for d, _ in kd.next_red_days(2, after, cal)] == ["2026-10-30", "2026-11-06"]
    assert kd.sessions_away("2026-10-26", "2026-10-30", cal) == 4
    assert kd.sessions_away("2026-10-30", "2026-10-30", cal) == 0
    assert kd.sessions_away("2026-11-25", "2026-11-30", cal) == 2, "Thanksgiving and the weekend skipped"


def _snap(ts, spot, rows, exps):
    return {"ts": ts, "spot": spot, "exp": exps,
            "fields": ["strike", "exp_idx", "side", "oi", "volume", "gamma", "iv", "delta", "bid", "ask", "mark"],
            "rows": rows}


def test_positioning_follows_one_expiry_session_by_session(cal, tmp_path):
    exps = ["2026-10-23", "2026-10-30"]
    day1 = [_snap("2026-10-22T10:00:00-04:00", 7700.0, [[7700.0, 1, 0, 1, 0, 0, 15.0, 0, 0, 0, 0]], exps),
            _snap("2026-10-22T15:56:00-04:00", 7700.0, [[7700.0, 1, 0, 100, 0, 0, 15.0, 0, 0, 0, 0],
                                                        [7700.0, 1, 1, 40, 0, 0, 16.0, 0, 0, 0, 0],
                                                        [7700.0, 0, 0, 999, 0, 0, 15.0, 0, 0, 0, 0],     # other expiry
                                                        [9000.0, 1, 0, 5, 0, 0, 15.0, 0, 0, 0, 0]], exps)]   # outside band
    with gzip.open(tmp_path / "gex_full_SPX_2026-10-22.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(s) for s in day1) + "\n")
    day2 = _snap("2026-10-23T12:00:00-04:00", 7710.0, [[7700.0, 0, 0, 1, 0, 0, 15.0, 0, 0, 0, 0],
                                                       [7700.0, 1, 0, 150, 0, 0, 15.0, 0, 0, 0, 0],
                                                       [7650.0, 1, 1, 60, 0, 0, 18.0, 0, 0, 0, 0]], exps)
    (tmp_path / "gex_full_SPX_2026-10-23.jsonl").write_text(json.dumps(day2) + "\n" + '{"ts": "2026-10-23T12:15', encoding="utf-8")
    p = kd.positioning("2026-10-30", str(tmp_path))
    assert p["strikes"] == [7650.0, 7700.0]
    s1, s2 = p["sessions"]
    assert (s1["day"], s1["ts"][11:16]) == ("2026-10-22", "15:56"), "the session's last snapshot"
    assert s1["call_oi"] == [0, 100] and s1["put_oi"] == [0, 40]
    assert (s2["day"], s2["ts"][11:16]) == ("2026-10-23", "12:00"), "half-written line skipped"
    assert s2["call_oi"] == [0, 150] and s2["put_oi"] == [60, 0]
    when = datetime.fromisoformat(s1["ts"])
    T = bs.t_to_close("2026-10-30", now=when)
    g = lambda iv: bs._bs_gamma(7700.0, 7700.0, T, kd.RISK_FREE, iv)
    assert s1["gex"][1] == round((g(0.15) * 100 - g(0.16) * 40) * 100 * 7700.0)
    assert kd.positioning("2026-11-20", str(tmp_path)) == {"strikes": [], "sessions": []}


def test_summary(cal, tmp_path):
    s = kd.summary(2, datetime(2026, 10, 26, 17, 0, tzinfo=ET), str(tmp_path), cal, notes_file=str(tmp_path / "notes.jsonl"))
    assert [n["date"] for n in s["next"]] == ["2026-10-28", "2026-10-30"]
    assert s["next"][0]["sessions_away"] == 2 and s["next"][0]["sessions"] == []
    assert "2026-12-31" in s["red"] and all(d >= "2026-10-26" for d in s["red"])


def test_expiry_stats_and_atm():
    cells = {7600.0: [0, 100, -50.0], 7650.0: [10, 40, -20.0], 7700.0: [80, 0, 90.0], 7750.0: [50, 0, 30.0]}
    st = kd.expiry_stats(cells, 7690.0)
    # max pain: puts 7600 x100, 7650 x40; calls 7650 x10, 7700 x80, 7750 x50 -> least paid out at 7650
    assert st["max_pain"] == 7650.0
    assert st["flip"] == 7700.0, "cumulative -50, -70, +20: turns positive at 7700"
    assert st["top_oi"] == [7600.0, 7700.0, 7650.0], "7650 and 7750 tie at 50: lower strike first"
    snap = _snap("2026-10-26T15:56:00-04:00", 7702.0, [[7700.0, 0, 0, 10, 0, 0, 14.0, 0, 9, 10, 9.5],
                                                       [7700.0, 0, 1, 10, 0, 0, 15.0, 0, 7, 8, 7.5],
                                                       [7705.0, 0, 0, 0, 0, 0, 13.0, 0, 6, 7, 6.5]], ["2026-10-28"])
    t = kd._table(snap)
    assert t["atm"]["2026-10-28"] == {"strike": 7700.0, "straddle": 17.0, "iv": 14.5}, "nearest strike with both sides"


def test_trade_sides(tmp_path):
    (tmp_path / "option_chain_SPX_2026-10-23.csv").write_text("\n".join([
        "time,expiry,dte,strike,side,root,bid,ask,last,mark,volume,oi,iv,delta,gamma,theta,vega,spot",
        "1,2026-10-28,5,7650.0,put,SPXW,9.0,10.0,9.5,9.5,0,0,0,0,0,0,0,0",
        "2,2026-10-28,5,7650.0,put,SPXW,9.0,10.0,10.0,9.5,100,0,0,0,0,0,0,0",     # +100 at the ask
        "3,2026-10-28,5,7650.0,put,SPXW,9.0,10.0,9.0,9.5,130,0,0,0,0,0,0,0",      # +30 at the bid
        "4,2026-10-28,5,7650.0,put,SPXW,9.0,10.0,9.5,9.5,140,0,0,0,0,0,0,0",      # +10 between
        "1,2026-11-20,25,7650.0,put,SPXW,9.0,10.0,9.5,9.5,0,0,0,0,0,0,0,0"]) + "\n", encoding="utf-8")
    sides = kd.trade_sides("2026-10-23", str(tmp_path), {"2026-10-28"})
    assert sides == {("2026-10-28", 7650.0, "p"): [100, 30, 10]}


def test_level_result():
    o = {"open": 7640.0, "high": 7702.0, "low": 7640.0, "close": 7677.0}
    assert kd.level_result(7630.0, 7624.0, o)[0] == "broke", "opened above it"
    assert kd.level_result(7600.0, 7624.0, o) == ("not reached", "low 7640")
    assert kd.level_result(7690.0, 7624.0, o) == ("held", "rejected, high 7702")
    assert kd.level_result(7650.0, 7660.0, {"open": 7655.0, "high": 7660.0, "low": 7645.0, "close": 7652.0})[0] == "held"
    assert kd.level_result(7650.0, 7660.0, {"open": 7655.0, "high": 7660.0, "low": 7640.0, "close": 7645.0})[0] == "broke"


def test_notes_scored_and_saved(cal, tmp_path, monkeypatch):
    exps = ["2026-10-23", "2026-10-30"]

    def day_file(day, spot, oi_put, mark):
        snap = _snap(f"{day}T15:56:00-04:00", spot, [[7700.0, 1, 0, 50, 0, 0, 15.0, 0, mark - 0.5, mark + 0.5, mark],
                                                     [7700.0, 1, 1, oi_put, 0, 0, 16.0, 0, mark - 0.5, mark + 0.5, mark],
                                                     [7650.0, 1, 1, 300, 0, 0, 18.0, 0, 1, 2, 1.5]], exps)
        (tmp_path / f"gex_full_SPX_{day}.jsonl").write_text(json.dumps(snap) + "\n", encoding="utf-8")
    day_file("2026-10-21", 7710.0, 40, 20.0)
    day_file("2026-10-22", 7705.0, 140, 22.0)
    bars = {"2026-10-22": [(9, 30, 7712, 7715, 7700, 7704), (15, 59, 7704, 7706, 7703, 7705)],
            "2026-10-23": [(9, 30, 7700, 7701, 7640, 7645), (15, 59, 7645, 7660, 7644, 7655)]}

    def load_range(sym, a, b=None, interval="1m"):
        if interval == "1d":
            return []
        out = []
        for day, rows in bars.items():
            for hh, mm, o, h, l, c in rows:
                t = int(datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00-04:00").timestamp() * 1000)
                if a <= t < b:
                    out.append({"datetime": t, "open": o, "high": h, "low": l, "close": c, "volume": 0})
        return out
    monkeypatch.setattr(kd, "load_range", load_range)
    pos = kd.positioning("2026-10-30", str(tmp_path))
    now = datetime(2026, 10, 23, 17, 0, tzinfo=ET)
    notes, rows, card = kd.build_notes("2026-10-30", ["MONTH END"], pos, now, str(tmp_path), cal)
    newest = [l["text"] for l in notes[0]["lines"]]
    assert notes[0]["day"] == "2026-10-22" and newest[0].startswith("OI 490 (+100)"), newest[0]
    assert any(t.startswith("Priced move to Fri 10/30: ±44 pts") for t in newest), newest
    assert any(t.startswith("Growing: 7700P +100") for t in newest), newest
    assert any(t.startswith("Biggest gamma below spot: 7650") for t in newest), newest
    assert any(t.startswith("Magnets: max pain") for t in newest)
    assert newest[-1].startswith("Next session (Fri 10/23): O 7700 H 7701 L 7640 C 7655"), newest[-1]
    assert "7650 held" in newest[-1], "low 7640 under 7650, closed 7655 back above it"
    assert rows[1]["outcome"]["below"] == "held" and card["neg"]["held"] == 1
    saved = kd.save_red_day_notes(now, str(tmp_path), cal, str(tmp_path / "notes.jsonl"))
    assert {(r["exp"], r["day"]) for r in saved} == {("2026-10-30", "2026-10-21"), ("2026-10-30", "2026-10-22")}
    again = kd.save_notes([], str(tmp_path / "notes.jsonl"))
    assert len(again) == 2, "kept on disk"
    assert kd.tally(again)["neg"]["held"] == 1


def test_event_history(cal, monkeypatch, tmp_path):
    events = tmp_path / "ev.csv"
    events.write_text("\n".join(["date,time_et,event,category,importance,source",
                                 "2026-09-16,14:00,FOMC decision,fomc,high,fed",
                                 "2026-10-28,14:00,FOMC decision,fomc,high,fed"]) + "\n", encoding="utf-8")

    def load_range(sym, a, b=None, interval="1m"):
        mk = lambda d, h, l, c: {"datetime": int(datetime.fromisoformat(f"{d}T00:00:00-04:00").timestamp() * 1000),
                                 "open": c, "high": h, "low": l, "close": c, "volume": 0}
        return [mk("2026-09-15", 6600, 6550, 6600), mk("2026-09-16", 6700, 6580, 6666)]
    monkeypatch.setattr(kd, "load_range", load_range)
    h = kd.event_history("fomc", "2026-10-26", str(events))
    assert h == {"n": 1, "move_pct": 1.0, "range": 120}
    info = kd.event_info("2026-10-28", ["FOMC"], "2026-10-26", str(events))
    assert info["text"].startswith("FOMC decision at 14:00 ET") and "±1.00%" in info["text"]


def test_save_notes_concurrent(tmp_path):
    # page requests save at the same time: none fails and every row ends up in the file
    import threading
    path = str(tmp_path / "notes.jsonl")
    errors = []

    def save(i):
        try:
            for j in range(20):
                kd.save_notes([{"exp": "2026-10-30", "day": f"2026-10-{i:02d}", "n": j}], path)
        except Exception as e:      # PermissionError on Windows before the lock
            errors.append(e)
    threads = [threading.Thread(target=save, args=(i,)) for i in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert {r["day"]: r["n"] for r in kd.save_notes([], path)} == {f"2026-10-{i:02d}": 19 for i in range(1, 9)}
