"""
Run from the project root:   python -m pytest tests/test_storage_levels.py -q
"""
import gzip
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import gex_stats as gs  # noqa: E402

ET = gs.ET


# ── gamma levels ─────────────────────────────────────────────────────

def test_gamma_levels_finds_each_level():
    strikes = [7700, 7720, 7740, 7760, 7780, 7800, 7810, 7820, 7830, 7840, 7860, 7880, 7900]
    gex = [-30, -5, 0.5, 0.5, 40, 100, 20, 60, 90, 10, 0.2, 0.3, -12]
    L = gs.gamma_levels(strikes, gex, spot=7825)
    assert [w["strike"] for w in L["walls"]] == [7800, 7830, 7820]
    assert L["trapdoor"]["strike"] == 7700          # 7720 (-5) is under the 10% bar; 7700 (-30) counts
    assert L["squeeze"]["strike"] == 7900           # first clearly negative above spot
    assert L["support"]["strike"] == 7800           # 7820 is within 7.5 pts of spot
    assert L["resistance"] is None                  # 7830 too close; 7840 (10) < 25% of max
    assert [7740, 7760] in L["air_pockets"] and [7860, 7880] in L["air_pockets"]
    assert L["brakes"]["label"] == "strong"         # halfway between 60 and 90 = 75% of 100


def test_support_resistance_are_the_biggest_walls_not_the_nearest():
    strikes = [7760, 7780, 7800, 7810, 7820, 7830, 7840, 7850, 7880]
    gex     = [10,   140,  60,   50,   80,   90,   45,   150,  70]
    L = gs.gamma_levels(strikes, gex, spot=7826)
    assert L["support"]["strike"] == 7780       # nearest qualifying would be 7810; 7780 is the big one
    assert L["resistance"]["strike"] == 7850    # nearest qualifying would be 7840


def test_brakes_accelerator_when_negative_at_spot():
    L = gs.gamma_levels([7790, 7800, 7810], [-20, -40, -10], spot=7800)
    assert L["brakes"]["label"] == "accelerator"
    assert L["support"] is None and L["resistance"] is None


# ── storage ──────────────────────────────────────────────────────────

def _grid(tmp, day, n=2):
    snaps = [{"ts": f"{day}T10:{i * 5:02d}:00-04:00", "spot": 7800.0, "exp": [day],
              "rows": [[7800.0, 0, 10, 0, 100.0 + i], [7750.0, 0, 0, 5, -20.0]]} for i in range(n)]
    path = tmp / f"gex_grid_SPX_{day}.jsonl"
    path.write_text("\n".join(json.dumps(s) for s in snaps), encoding="utf-8")
    return path


def test_archive_gzips_past_days_only_and_heatmap_still_reads(tmp_path):
    old, today = _grid(tmp_path, "2026-10-05"), _grid(tmp_path, "2026-10-06")
    original = old.read_bytes()
    done = gs.archive_old_files("SPX", today=date(2026, 10, 6), data_dir=tmp_path)
    assert done == ["gex_grid_SPX_2026-10-05.jsonl.gz"]
    assert not old.exists() and today.exists()
    assert gzip.decompress((tmp_path / done[0]).read_bytes()) == original
    h = gs.load_heatmap("SPX", day=date(2026, 10, 5), data_dir=tmp_path)
    assert h["all"] == [[-20, 100], [-20, 101]]
    assert gs.archive_old_files("SPX", today=date(2026, 10, 6), data_dir=tmp_path) == []   # idempotent


def _local(dt_et):
    return datetime.fromtimestamp(dt_et.timestamp()).strftime("%Y-%m-%d %H:%M:%S")


def test_daily_summary_row_is_replaced_not_duplicated(tmp_path):
    day = date(2026, 10, 5)
    rows = [{"timestamp": _local(datetime(2026, 10, 5, h, m, tzinfo=ET)), "spot": 7800.0,
             "total_gex": g, "regime": "", "flip_level": 7790, "put_wall": 7700, "call_wall": 7850,
             "pin": 7850, "strike_min": 7600, "strike_max": 8000, "true_pin": 7820}
            for h, m, g in ((9, 35, -1e8), (15, 55, 5e8))]
    pd.DataFrame(rows).to_csv(tmp_path / "gex_snapshots_SPX_2026-10-05.csv", index=False)
    bars = [{"datetime": int(datetime(2026, 10, 5, h, m, tzinfo=ET).timestamp() * 1000),
             "open": o, "high": o + 5, "low": o - 5, "close": o + 1, "volume": 0}
            for h, m, o in ((9, 30, 7790.0), (15, 59, 7810.0))]
    pd.DataFrame(bars).to_csv(tmp_path / "price_history_SPX.csv", index=False)
    _grid(tmp_path, "2026-10-05")

    r = gs.write_daily_summary(day, "SPX", tmp_path)
    gs.write_daily_summary(day, "SPX", tmp_path)
    df = pd.read_csv(tmp_path / "daily_summary_SPX.csv")
    assert len(df) == 1
    assert (r["open"], r["close"], r["high"], r["low"]) == (7790.0, 7811.0, 7815.0, 7785.0)
    assert (r["regime_open"], r["regime_close"]) == ("NEGATIVE", "POSITIVE")
    assert r["call_wall"] == 7850 and r["true_pin"] == 7820 and r["wall1"] == 7800.0
    assert gs.summary_days_missing("SPX", tmp_path) == []


# ── stickiness and scorecard helpers ─────────────────────────────────

def test_sticky_chain_levels_hold_near_equal_strikes():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from gex import sticky_levels
    df = pd.DataFrame({"strike": [7500.0, 7700.0, 7850.0, 7860.0], "net_gex": [-25.0, -24.0, 100.0, 108.0]})
    prev = {"put_wall": 7700.0, "call_wall": 7850.0, "pin": 7850.0}
    new = {"put_wall": 7500.0, "call_wall": 7860.0, "pin": 7860.0, "flip_level": 7800.0}
    out = sticky_levels(new, prev, df, spot=7820)
    assert (out["put_wall"], out["call_wall"], out["pin"]) == (7700.0, 7850.0, 7850.0)   # within 15%: keep
    df.loc[df.strike == 7860.0, "net_gex"] = 130.0
    assert sticky_levels(new, prev, df, spot=7820)["call_wall"] == 7860.0              # 30% bigger: move
    assert sticky_levels(new, None, df, spot=7820) == new


def test_gamma_levels_prev_keeps_support_and_trapdoor():
    strikes = [7680, 7700, 7720, 7790, 7800, 7810, 7830, 7850]
    gex     = [-30,  -28,  -2,   90,   100,  20,   60,   95]
    first = gs.gamma_levels(strikes, gex, spot=7825)
    assert first["support"]["strike"] == 7800 and first["trapdoor"]["strike"] == 7700
    gex2 = [-30, -27, -2, 104, 100, 20, 60, 95]                      # 7790 now slightly bigger than 7800
    assert gs.gamma_levels(strikes, gex2, 7825)["support"]["strike"] == 7790
    kept = gs.gamma_levels(strikes, gex2, 7825, prev={"support": 7800, "trapdoor": 7700})
    assert kept["support"]["strike"] == 7800 and kept["trapdoor"]["strike"] == 7700


def test_wall_reaction_touch_and_break():
    px = pd.DataFrame({"high": [7840, 7848.5, 7845], "low": [7830, 7838, 7832], "close": [7838, 7846, 7840]})
    assert gs.wall_reaction(px, 7850, "up") == {"touch": 1, "break": 0, "gap": 1.5}
    assert gs.wall_reaction(px, 7845, "up")["break"] == 0      # closes 7846: only 1 pt through
    assert gs.wall_reaction(px, 7843, "up")["break"] == 1
    assert gs.wall_reaction(px, 7800, "down")["touch"] == 0


# ── price history: never save an unfinished minute; repair overwrites ─

def test_finished_drops_the_minute_in_progress(tmp_path, monkeypatch):
    import price_history as ph
    now = 1_791_400_000_000
    bars = [{"datetime": now - 120_000}, {"datetime": now - 60_000}, {"datetime": now - 30_000}]
    assert [b["datetime"] for b in ph.finished(bars, now_ms=now)] == [now - 120_000, now - 60_000]


def test_replace_candles_overwrites_partial_minutes(tmp_path, monkeypatch):
    import price_history as ph
    monkeypatch.setattr(ph, "DATA_DIR", str(tmp_path))
    t = 1_700_000_000_000
    bar = lambda dt, c, v: {"datetime": dt, "open": 1.0, "high": c, "low": 1.0, "close": c, "volume": v}
    ph.save_candles("/ES", [bar(t, 2.0, 100), bar(t + 60_000, 3.0, 50)])          # second one partial
    ph.append_candles("/ES", [bar(t + 60_000, 9.0, 999)])                         # append can't fix it
    assert ph.load_candles("/ES")[1]["volume"] == 50
    assert ph.replace_candles("/ES", [bar(t + 60_000, 3.5, 400), bar(t + 120_000, 4.0, 10)]) == 2
    rows = ph.load_candles("/ES")
    assert [(r["close"], r["volume"]) for r in rows] == [(2.0, 100), (3.5, 400), (4.0, 10)]


def test_append_dedups_from_the_index_and_sees_outside_writes(tmp_path, monkeypatch):
    import price_history as ph
    monkeypatch.setattr(ph, "DATA_DIR", str(tmp_path))
    t = 1_700_000_000_000
    bar = lambda dt, c: {"datetime": dt, "open": c, "high": c, "low": c, "close": c, "volume": 7.0}
    assert ph.append_candles("/ES", [bar(t, 1.0), bar(t + 60_000, 2.0)]) == 2
    assert [r["datetime"] for r in ph.append_new("/ES", [bar(t + 60_000, 9.0), bar(t + 120_000, 3.0)])] == [t + 120_000]
    assert ph.append_candles("/ES", [bar(t, 5.0)]) == 0                    # already saved
    assert ph.last_saved_ms("/ES") == t + 120_000
    with open(ph.csv_path("/ES"), "ab") as f:                             # another process appends,
        f.write(f"{t + 180_000},4,4,4,4,1".encode())                      # no newline at the end
    assert ph.append_candles("/ES", [bar(t + 180_000, 8.0), bar(t + 240_000, 5.0)]) == 1
    rows = ph.load_candles("/ES")
    assert [r["close"] for r in rows] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert rows[0]["volume"] == 7                                         # 7.0 saved as an int
    assert [r["close"] for r in ph.load_range("/ES", t + 60_000, t + 180_000)] == [2.0, 3.0]


def test_replace_candles_streams_and_keeps_other_rows(tmp_path, monkeypatch):
    import price_history as ph
    monkeypatch.setattr(ph, "DATA_DIR", str(tmp_path))
    t = 1_700_000_000_000
    bar = lambda dt, c, v=1: {"datetime": dt, "open": c, "high": c, "low": c, "close": c, "volume": v}
    ph.append_candles("/ES", [bar(t + i * 60_000, float(i)) for i in range(5)])
    assert ph.replace_candles("/ES", [bar(t + 60_000, 1.0)]) == 0          # same values: no rewrite
    assert ph.replace_candles("/ES", [bar(t + 120_000, 2.5, 9), bar(t + 600_000, 10.0)]) == 2
    rows = ph.load_candles("/ES")
    assert [(r["close"], r["volume"]) for r in rows] == [(0.0, 1), (1.0, 1), (2.5, 9), (3.0, 1), (4.0, 1), (10.0, 1)]
    assert ph.append_candles("/ES", [bar(t + 600_000, 0.0)]) == 0          # index rebuilt after rewrite
