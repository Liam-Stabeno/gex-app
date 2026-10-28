"""
Run from the project root:   python -m pytest tests/test_gex_horizon.py -q
"""
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import gex_stats as gs  # noqa: E402


def test_gex_by_expiry_later_expirations_per_strike():
    import background
    raw = pd.DataFrame([
        # expiration, is_0dte, strike, type, gex
        ("2026-10-26", True, 7700.0, "call", 999.0),       # today's 0DTE: the panel adds the live one
        ("2026-10-28", False, 7690.0, "put", -30.0),
        ("2026-10-28", False, 7700.0, "call", 50.0),
        ("2026-10-28", False, 7700.0, "put", -20.0),
        ("2026-10-27", False, 7710.0, "call", 7.0),
        ("2026-10-27", False, 7800.0, "call", 0.0),        # no GEX at the edge: trimmed
        ("2026-10-30", False, 7600.0, "put", 0.0),
    ], columns=["expiration", "is_0dte", "strike", "type", "gex"])
    out = background.gex_by_expiry(raw)
    assert out["exp"] == ["2026-10-27", "2026-10-28", "2026-10-30"]
    assert out["strikes"] == [7690.0, 7700.0, 7710.0]
    assert out["gex"] == [[0, 0, 7], [-30, 30, 0], [0, 0, 0]]
    assert background.gex_by_expiry(raw[raw["is_0dte"]]) == {"exp": [], "strikes": [], "gex": []}
    assert background.gex_by_expiry(None)["exp"] == []


def test_week_end():
    assert gs.week_end("2026-10-26") == "2026-10-30"     # Monday -> Friday
    assert gs.week_end("2026-10-30") == "2026-10-30"     # Friday: itself
    assert gs.week_end("2026-10-31") == "2026-10-31"     # weekend: itself (no session)


def test_heatmap_week_column_counts_expirations_through_friday(tmp_path):
    snap = {"ts": "2026-10-26T10:00:00-04:00", "spot": 7700.0,
            "exp": ["2026-10-26", "2026-10-28", "2026-10-30", "2026-11-02"],
            "rows": [[7700.0, 0, 1, 1, 10.0], [7700.0, 1, 1, 1, 20.0], [7700.0, 2, 1, 1, 30.0],
                     [7700.0, 3, 1, 1, 40.0], [7710.0, 3, 1, 0, 5.0]]}
    (tmp_path / "gex_grid_SPX_2026-10-26.jsonl").write_text(json.dumps(snap) + "\n", encoding="utf-8")
    h = gs.load_heatmap("SPX", day=date(2026, 10, 26), data_dir=tmp_path)
    assert h["strikes"] == [7700.0, 7710.0]
    assert h["all"] == [[100, 5]] and h["odte"] == [[10, 0]]
    assert h["week"] == [[60, 0]], "today + Wednesday + Friday, not next Monday"
    levels = gs.current_gamma_levels(7700.0, "week", data_dir=tmp_path, day=date(2026, 10, 26))
    assert levels is not None


def test_heatmap_red_day_columns(tmp_path, monkeypatch):
    import key_dates
    monkeypatch.setattr(key_dates, "next_red_days", lambda n=2, now=None, path=None:
                        [("2026-10-28", ["FOMC"]), ("2026-10-30", ["MONTH END"])][:n])
    snaps = [{"ts": f"2026-10-26T10:0{m}:00-04:00", "spot": 7700.0,
              "exp": ["2026-10-26", "2026-10-28", "2026-10-30"],
              "rows": [[7700.0, 0, 1, 1, 10.0], [7700.0, 1, 1, 1, 20.0 + m], [7710.0, 2, 1, 0, 5.0]]}
             for m in range(2)]
    (tmp_path / "gex_grid_SPX_2026-10-26.jsonl").write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")
    h = gs.load_heatmap("SPX", day=date(2026, 10, 26), data_dir=tmp_path, red=2)
    assert [r["key"] for r in h["red"]] == ["x:2026-10-28", "x:2026-10-30"]
    assert h["strikes"] == [7700.0, 7710.0]
    assert h["x:2026-10-28"] == [[20, 0], [21, 0]], "that expiration only, minute by minute"
    assert h["x:2026-10-30"] == [[0, 5], [0, 5]]
    assert "x:2026-10-28" not in gs.load_heatmap("SPX", day=date(2026, 10, 26), data_dir=tmp_path)
    assert gs.current_gamma_levels(7700.0, "x:2026-10-28", data_dir=tmp_path, day=date(2026, 10, 26)) is not None
    assert gs.current_gamma_levels(7700.0, "x:2026-11-20", data_dir=tmp_path, day=date(2026, 10, 26)) is None
    pre = gs.load_heatmap("SPX", day=date(2026, 10, 27), data_dir=tmp_path, red=2)
    assert pre["times"] == [] and [r["key"] for r in pre["red"]] == ["x:2026-10-28", "x:2026-10-30"], \
        "before the first snapshot: no columns, but the red days (the page's buttons)"
    assert pre["x:2026-10-28"] == []


# ── The live 0DTE (stream) in the heatmap and the levels ───────────────────────

def _live_snap(m, live0=None):
    s = {"ts": f"2026-10-28T10:0{m}:00-04:00", "spot": 7700.0,
         "exp": ["2026-10-28", "2026-10-30", "2026-11-02"],
         "rows": [[7690.0, 0, 1, 1, -5.0], [7700.0, 0, 1, 1, 10.0], [7700.0, 1, 1, 1, 20.0],
                  [7700.0, 2, 1, 1, 40.0], [7710.0, 0, 1, 0, 3.0]]}
    if live0 is not None:
        s["live0"] = live0
    return s


def test_heatmap_reads_the_live_0dte_over_the_chains(tmp_path, monkeypatch):
    import key_dates
    monkeypatch.setattr(key_dates, "next_red_days", lambda n=2, now=None, path=None: [("2026-10-28", ["FOMC"])][:n])
    snaps = [_live_snap(0), _live_snap(1, [[7700.0, 14.0], [7720.0, -2.0]])]   # older line: chain only
    (tmp_path / "gex_grid_SPX_2026-10-28.jsonl").write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")
    h = gs.load_heatmap("SPX", day=date(2026, 10, 28), data_dir=tmp_path, red=1)
    assert h["strikes"] == [7690.0, 7700.0, 7710.0, 7720.0]
    assert h["odte"] == [[-5, 10, 3, 0], [-5, 14, 3, -2]], "live where it has the strike, else the chain's"
    assert h["all"] == [[-5, 70, 3, 0], [-5, 74, 3, -2]], "the other expirations unchanged"
    assert h["week"] == [[-5, 30, 3, 0], [-5, 34, 3, -2]]
    assert h["x:2026-10-28"] == h["odte"], "today's red-day column is today's 0DTE: live too"


def test_gamma_levels_take_the_live_0dte_now(tmp_path):
    snaps = [_live_snap(m) for m in range(2)]
    (tmp_path / "gex_grid_SPX_2026-10-28.jsonl").write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")
    day = date(2026, 10, 28)
    chain = gs.current_gamma_levels(7705.0, "odte", data_dir=tmp_path, day=day)
    assert chain["trapdoor"]["strike"] == 7690.0 and chain["live"] is False
    live = gs.current_gamma_levels(7705.0, "odte", data_dir=tmp_path, day=day, live={7690.0: 0.0, 7700.0: -30.0})
    assert live["trapdoor"]["strike"] == 7700.0 and live["live"] is True
    assert live["history"] == chain["history"], "the trail stays the snapshots'"
    every = gs.current_gamma_levels(7705.0, "all", data_dir=tmp_path, day=day, live={7700.0: -100.0})
    assert every["trapdoor"]["strike"] == 7700.0, "all = the latest snapshot with its 0DTE swapped for live (-100 + 60)"
    assert every["trapdoor"]["gex"] == -40.0


def test_record_gex_grid_saves_the_live_0dte(tmp_path, monkeypatch):
    import time
    from datetime import datetime
    import background as bg

    class Session(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 29, 11, 0, tzinfo=tz)
    monkeypatch.setattr(bg, "datetime", Session)
    monkeypatch.setattr(bg, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bg, "_grid_last_ts", {})
    raw = pd.DataFrame([("2026-10-29", 7700.0, "call", 10, 50.0), ("2026-10-29", 7700.0, "put", 5, -20.0)],
                       columns=["expiration", "strike", "type", "oi", "gex"])
    monkeypatch.setattr(bg, "_live_0dte", {"SPX": (time.time(), {7700.0: 33.4, 7695.0: -1.2})})
    bg._record_gex_grid("SPX", raw, 7700.0)
    monkeypatch.setattr(bg, "_live_0dte", {"SPX": (time.time() - 60, {7700.0: 1.0})})   # stream gone quiet
    bg._grid_last_ts.clear()
    bg._record_gex_grid("SPX", raw, 7700.0)
    lines = [json.loads(l) for l in (tmp_path / "gex_grid_SPX_2026-10-29.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[0]["rows"] == [[7700.0, 0, 10, 5, 30]], "the chain's cells as before"
    assert lines[0]["live0"] == [[7695.0, -1], [7700.0, 33]]
    assert "live0" not in lines[1], "a stale live 0DTE isn't saved"
