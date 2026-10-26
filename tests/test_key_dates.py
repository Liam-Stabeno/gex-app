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
    s = kd.summary(2, datetime(2026, 10, 26, 17, 0, tzinfo=ET), str(tmp_path), cal)
    assert [n["date"] for n in s["next"]] == ["2026-10-28", "2026-10-30"]
    assert s["next"][0]["sessions_away"] == 2 and s["next"][0]["sessions"] == []
    assert "2026-12-31" in s["red"] and all(d >= "2026-10-26" for d in s["red"])
