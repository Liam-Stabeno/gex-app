"""
Run from the project root:   python -m pytest tests/test_rebuild_gex_grid.py -q
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
import bs                    # noqa: E402
import gex_stats as gs       # noqa: E402
import rebuild_gex_grid as rb  # noqa: E402

ET = rb.ET
DAY = "2026-10-19"


def ts(hm: str) -> int:
    return int(datetime.fromisoformat(f"{DAY}T{hm}:00-04:00").timestamp())


@pytest.fixture
def day_files(tmp_path, monkeypatch):
    """Recorded from 12:56; full chain at 12:59 with a 0DTE call/put at 7700 and a next-day call at 7750."""
    grid = {"ts": f"{DAY}T12:56:51-04:00", "spot": 7702.0, "exp": [DAY, "2026-10-20"],
            "rows": [[7700.0, 0, 100, 200, -1.0], [7750.0, 1, 50, 0, 1.0]]}
    (tmp_path / f"gex_grid_SPX_{DAY}.jsonl").write_text(json.dumps(grid) + "\n" + json.dumps(
        {**grid, "ts": f"{DAY}T12:57:51-04:00"}) + "\n", encoding="utf-8")
    full = {"ts": f"{DAY}T12:59:46-04:00", "spot": 7702.0, "exp": [DAY, "2026-10-20", "2026-12-18"],
            "fields": ["strike", "exp_idx", "side", "oi", "volume", "gamma", "iv", "delta", "bid", "ask", "mark"],
            "rows": [[7700.0, 0, 0, 100, 5, 0.017, 15.0, 0.5, 9, 10, 9.5],
                     [7700.0, 0, 1, 200, 5, 0.017, 16.0, -0.5, 8, 9, 8.5],
                     [7750.0, 1, 0, 50, 1, 0.004, 14.0, 0.3, 20, 21, 20.5],
                     [7900.0, 2, 0, 999, 1, 0.001, 13.0, 0.1, 5, 6, 5.5],    # not in the grid: left out
                     [7650.0, 0, 1, 0, 9, 0.010, 18.0, -0.2, 2, 3, 2.5]]}    # no OI: left out
    (tmp_path / f"gex_full_SPX_{DAY}.jsonl").write_text(json.dumps(full) + "\n", encoding="utf-8")
    candles = [{"datetime": ts(hm) * 1000, "close": px} for hm, px in
               (("09:30", 7690.0), ("09:31", 7695.0), ("12:55", 7701.0), ("12:56", 7702.0))]
    monkeypatch.setattr(rb, "load_range", lambda sym, a, b: [c for c in candles if a <= c["datetime"] < b])
    return tmp_path


def gex_at(rows, exps, exp, k):
    return next(r[4] for r in rows if exps[r[1]] == exp and r[0] == k)


def test_rebuilds_only_the_minutes_before_recording(day_files):
    path = rb.rebuild(DAY, str(day_files))
    lines = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()]
    # 12:56's minute closes at 12:56:59, after recording started at 12:56:51
    assert [l["ts"][11:19] for l in lines] == ["09:30:59", "09:31:59", "12:55:59"]
    first = lines[0]
    assert first["spot"] == 7690.0 and first["rebuilt"]["contracts"] == 3
    assert {(first["exp"][r[1]], r[0]): (r[2], r[3]) for r in first["rows"]} == {
        (DAY, 7700.0): (100, 200), ("2026-10-20", 7750.0): (50, 0)}
    when = datetime.fromisoformat(first["ts"])
    T0 = bs.t_to_close(DAY, now=when)
    g = lambda iv: round(bs._bs_gamma(7690.0, 7700.0, T0, rb.RISK_FREE, iv), 3)   # Schwab's 3 decimals
    assert gex_at(first["rows"], first["exp"], DAY, 7700.0) == round((g(0.15) * 100 - g(0.16) * 200) * 100 * 7690.0)
    assert (day_files / f"gex_grid_SPX_{DAY}.jsonl").read_text().count("\n") == 2, "recorded file untouched"


def test_vol_from_trade_bars_when_saved(day_files):
    T = bs.t_to_close(DAY, now=datetime.fromisoformat(f"{DAY}T09:30:59-04:00"))
    px = bs._bs_price(7690.0, 7700.0, T, rb.RISK_FREE, 0.25, "c")               # a 25% vol trade at 9:30
    (day_files / f"option_trade_bars_SPX_{DAY}.csv").write_text(
        "time,symbol,expiry,strike,side,open,high,low,close,volume\n"
        f"{ts('09:30')},SPXW  261019C07700000,{DAY},7700.0,call,{px},{px},{px},{px},3\n", encoding="utf-8")
    lines = [json.loads(l) for l in Path(rb.rebuild(DAY, str(day_files))).read_text().splitlines()]
    assert [l["rebuilt"]["iv_from_trades"] for l in lines] == [1, 1, 1], "held from the last trade"
    when = datetime.fromisoformat(lines[0]["ts"])
    T0 = bs.t_to_close(DAY, now=when)
    g = lambda iv: round(bs._bs_gamma(7690.0, 7700.0, T0, rb.RISK_FREE, iv), 3)
    assert gex_at(lines[0]["rows"], lines[0]["exp"], DAY, 7700.0) == round((g(0.25) * 100 - g(0.16) * 200) * 100 * 7690.0)


def test_check_runs(day_files, capsys):
    rb.check(DAY, str(day_files))
    out = capsys.readouterr().out
    assert "recorded" in out and "rebuilt" in out


def test_heatmap_puts_rebuilt_minutes_first_for_the_chart_only(day_files, monkeypatch):
    monkeypatch.setattr(gs, "_hm_cache", {})
    rb.rebuild(DAY, str(day_files))
    rec = gs.load_heatmap("SPX", day=date(2026, 10, 19), data_dir=day_files)
    assert len(rec["times"]) == 2 and rec["rebuilt_until"] is None, "stats: recorded only"
    h = gs.load_heatmap("SPX", day=date(2026, 10, 19), data_dir=day_files, rebuilt=True)
    assert len(h["times"]) == 5 and h["times"] == sorted(h["times"])
    assert h["rebuilt_until"] == ts("12:55") + 59
    assert h["times"][3] == ts("12:56") + 51                                  # recorded from here
    # rebuilt again (e.g. with trade bars): picked up without a restart
    p = day_files / f"gex_grid_rebuilt_SPX_{DAY}.jsonl"
    p.write_text(p.read_text().splitlines()[0] + "\n", encoding="utf-8")
    h2 = gs.load_heatmap("SPX", day=date(2026, 10, 19), data_dir=day_files, rebuilt=True)
    assert len(h2["times"]) == 3 and h2["rebuilt_until"] == ts("09:30") + 59
