"""
Run from the project root:   python -m pytest tests/test_gex_stats.py -q
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import gex_stats as gs  # noqa: E402

ET = gs.ET


def _local(dt_et: datetime) -> str:
    """Snapshots store naive local time."""
    return datetime.fromtimestamp(dt_et.timestamp()).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def history(tmp_path, monkeypatch):
    """6 positive-GEX days that close 5 pts from the 14:00 spot, 6 negative days 30 pts away."""
    monkeypatch.setitem(gs._table_cache, "table", None)
    candles = []
    day = datetime(2026, 9, 1, tzinfo=ET)
    for i in range(12):
        d = day + timedelta(days=i)
        pos = i < 6
        spot, close = 7800.0, 7800.0 + (5 if pos else -30)
        rows = [{"timestamp": _local(d.replace(hour=h, minute=m)), "spot": spot,
                 "total_gex": 5e8 if pos else -5e8, "flip_level": "", "put_wall": 7700,
                 "call_wall": 7850, "pin": 7850}
                for h, m in ((9, 45), (13, 58), (14, 59))]
        pd.DataFrame(rows).to_csv(tmp_path / f"gex_snapshots_SPX_{d.date()}.csv", index=False)
        bar = d.replace(hour=15, minute=59)
        candles.append({"datetime": int(bar.timestamp() * 1000), "open": close, "high": close,
                        "low": close, "close": close, "volume": 0})
    pd.DataFrame(candles).to_csv(tmp_path / "price_history_SPX.csv", index=False)
    return tmp_path


def test_table_by_regime(history):
    table, days = gs.expected_move_table(history)
    assert days == 12
    assert table.loc[("POSITIVE", "14:00"), "median"] == 5
    assert table.loc[("NEGATIVE", "14:00"), "median"] == 30
    assert ("POSITIVE", "12:00") not in table.index      # snapshot 2h old: too stale


def test_now_lookup(history):
    at = lambda h, m: datetime(2026, 10, 6, h, m, tzinfo=ET)
    em = gs.expected_move_now(5e8, at(14, 30), history)
    assert em["regime"] == "POSITIVE" and em["from"] == "14:00" and em["median"] == 5 and em["n"] == 6
    assert gs.expected_move_now(-5e8, at(15, 10), history)["median"] == 30
    assert gs.expected_move_now(5e8, at(17, 0), history) is None     # after the close
    assert gs.expected_move_now(5e8, at(12, 30), history) is None    # no 12:00 history


def test_heatmap_sums_expiries_and_splits_0dte(tmp_path):
    import json
    from datetime import date
    day = date(2026, 10, 6)
    snaps = [{"ts": "2026-10-06T10:00:00-04:00", "spot": 7800.0,
              "exp": ["2026-10-06", "2026-10-07"],
              "rows": [[7800.0, 0, 10, 5, 100.0], [7800.0, 1, 3, 1, 50.0],
                       [7750.0, 0, 0, 9, -40.0], [9000.0, 1, 1, 0, 7.0]]},   # 9000: outside band
             {"ts": "2026-10-06T10:05:00-04:00", "spot": 7805.0,
              "exp": ["2026-10-06", "2026-10-07"], "rows": [[7800.0, 1, 3, 1, 60.0]]}]
    (tmp_path / "gex_grid_SPX_2026-10-06.jsonl").write_text("\n".join(json.dumps(s) for s in snaps))
    h = gs.load_heatmap("SPX", day=day, data_dir=tmp_path)
    assert h["strikes"] == [7750.0, 7800.0]
    assert h["all"] == [[-40, 150], [0, 60]]
    assert h["odte"] == [[-40, 100], [0, 0]]
    assert h["times"][1] - h["times"][0] == 300
    assert gs.load_heatmap("SPX", day=date(2026, 1, 2), data_dir=tmp_path)["times"] == []


def test_heatmap_cache_reads_only_appended_lines(tmp_path):
    import json
    from datetime import date
    path = tmp_path / "gex_grid_SPX_2026-10-06.jsonl"
    snap = lambda m, g: json.dumps({"ts": f"2026-10-06T10:{m:02d}:00-04:00", "spot": 7800.0,
                                    "exp": ["2026-10-06"], "rows": [[7800.0, 0, 1, 0, g]]}) + "\n"
    path.write_text(snap(0, 10.0), encoding="utf-8")
    assert gs.load_heatmap("SPX", day=date(2026, 10, 6), data_dir=tmp_path)["all"] == [[10]]
    with open(path, "a", encoding="utf-8") as f:
        f.write(snap(1, 20.0))
        f.write('{"ts": "2026-10-06T10:02')            # being written: must not appear yet
    assert gs.load_heatmap("SPX", day=date(2026, 10, 6), data_dir=tmp_path)["all"] == [[10], [20]]
    with open(path, "a", encoding="utf-8") as f:
        f.write(':00-04:00", "spot": 7801.0, "exp": ["2026-10-06"], "rows": [[7800.0, 0, 1, 0, 30.0]]}\n')
    assert gs.load_heatmap("SPX", day=date(2026, 10, 6), data_dir=tmp_path)["all"] == [[10], [20], [30]]


def test_heatmap_leaves_out_columns_after_the_close(tmp_path):
    import json
    from datetime import date
    snap = lambda hm, g: json.dumps({"ts": f"2026-10-08T{hm}:00-04:00", "spot": 7765.0,
                                     "exp": ["2026-10-08"], "rows": [[7765.0, 0, 1, 0, g]]})
    (tmp_path / "gex_grid_SPX_2026-10-08.jsonl").write_text(
        "\n".join([snap("15:59", -140e6), snap("16:00", -343e6), snap("16:13", -351e6)]))
    h = gs.load_heatmap("SPX", day=date(2026, 10, 8), data_dir=tmp_path)
    assert h["all"] == [[-140_000_000]]          # expired 0DTE after 16:00 stays in the file only


def test_finished_day_heatmap_kept_as_its_result(tmp_path, monkeypatch):
    import gzip, json, os
    from datetime import date
    monkeypatch.setattr(gs, "_hm_done", {})
    snap = lambda m, g: json.dumps({"ts": f"2026-10-06T10:{m:02d}:00-04:00", "spot": 7800.0,
                                    "exp": ["2026-10-06"], "rows": [[7800.0, 0, 1, 0, g]]})
    plain = tmp_path / "gex_grid_SPX_2026-10-06.jsonl"
    plain.write_text(snap(0, 10.0) + "\n", encoding="utf-8")
    gs.load_heatmap("SPX", day=date(2026, 10, 6), data_dir=tmp_path)          # read while live
    assert str(plain) in gs._hm_cache
    gz = Path(str(plain) + ".gz")                                              # the nightly archive
    with gzip.open(gz, "wt", encoding="utf-8") as f:
        f.write(snap(0, 10.0) + "\n" + snap(1, 20.0) + "\n")
    plain.unlink()
    parsed = []
    real = gs._hm_parse
    monkeypatch.setattr(gs, "_hm_parse", lambda *a: parsed.append(1) or real(*a))
    h = gs.load_heatmap("SPX", day=date(2026, 10, 6), data_dir=tmp_path)
    assert h["all"] == [[10], [20]] and len(parsed) == 2
    assert str(plain) not in gs._hm_cache and str(gz) not in gs._hm_cache, "no raw snapshots kept"
    assert gs.load_heatmap("SPX", day=date(2026, 10, 6), data_dir=tmp_path) is h
    assert len(parsed) == 2, "the second load is the cached result"
    with gzip.open(gz, "wt", encoding="utf-8") as f:                           # rewritten: read again
        f.write(snap(0, 99.0) + "\n")
    os.utime(gz, (1, 1))
    assert gs.load_heatmap("SPX", day=date(2026, 10, 6), data_dir=tmp_path)["all"] == [[99]]


def test_finished_day_cache_is_bounded(tmp_path, monkeypatch):
    import gzip, json
    from datetime import date
    monkeypatch.setattr(gs, "_hm_done", {})
    monkeypatch.setattr(gs, "HM_DONE_MAX", 3)
    for d in range(1, 6):
        day = f"2026-09-{d:02d}"
        with gzip.open(tmp_path / f"gex_grid_SPX_{day}.jsonl.gz", "wt", encoding="utf-8") as f:
            f.write(json.dumps({"ts": f"{day}T10:00:00-04:00", "spot": 7800.0,
                                "exp": [day], "rows": [[7800.0, 0, 1, 0, d]]}) + "\n")
        assert gs.load_heatmap("SPX", day=date(2026, 9, d), data_dir=tmp_path)["all"] == [[d]]
    assert len(gs._hm_done) == 3
    assert sorted(k[0][-19:-9] for k in gs._hm_done) == ["2026-09-03", "2026-09-04", "2026-09-05"]


def test_fix_charm_scores_once(tmp_path):
    # rows scored on the backwards charm sign get calls - hits, once; marked rows stay
    pd.DataFrame({"date": ["2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31"],
                  "charm_hits": [0, 1, 2, ""], "charm_calls": [3, 2, 3, ""], "charm_sign": ["", "", "dealer", ""]}
                 ).to_csv(tmp_path / "scorecard_SPX.csv", index=False)
    assert gs.fix_charm_scores("SPX", data_dir=tmp_path) == 2
    df = pd.read_csv(tmp_path / "scorecard_SPX.csv", dtype=str)
    assert list(df["charm_hits"][:3]) == ["3", "1", "2"] and list(df["charm_sign"][:3]) == ["dealer"] * 3
    assert pd.isna(df["charm_sign"][3])                    # no charm calls that day: left alone
    assert gs.fix_charm_scores("SPX", data_dir=tmp_path) == 0
    assert gs.heatmap_reliability("SPX", data_dir=tmp_path)["charm"] == {"hits": 6, "calls": 8}


def test_heatmap_reliability_by_regime(tmp_path):
    pd.DataFrame({"date": ["2026-10-06", "2026-10-07", "2026-10-08"],
                  "speed_ratio_pos": [1.97, 1.47, ""], "speed_ratio_neg": ["", "", 0.93],
                  "neg_share": [0.0, 0.1, 0.9], "charm_hits": ["", 1, 0], "charm_calls": ["", 2, 3]}
                 ).to_csv(tmp_path / "scorecard_SPX.csv", index=False)
    r = gs.heatmap_reliability("SPX", data_dir=tmp_path)
    assert r["pos"] == {"ratio": 1.72, "n": 2} and r["neg"] == {"ratio": 0.93, "n": 1}
    assert r["days"]["2026-10-08"]["neg_share"] == 0.9 and r["charm"] == {"hits": 1, "calls": 5}
    assert gs.heatmap_reliability("SPX", data_dir=tmp_path / "none") == {"pos": None, "neg": None, "days": {}, "charm": None}


def test_readers_follow_a_file_archived_after_lookup(tmp_path):
    """The archive runs at startup; a request that looked a day file up just before it was
    gzipped still reads it."""
    import gzip
    import json
    plain = tmp_path / "gex_grid_rebuilt_SPX_2026-10-26.jsonl"
    line = json.dumps({"ts": "2026-10-26T09:31:00-04:00", "spot": 7700.0, "exp": ["2026-10-26"],
                       "rows": [[7700.0, 0, 10, 5, 1.5e6]]}) + "\n"
    with gzip.open(str(plain) + ".gz", "wt", encoding="utf-8") as f:
        f.write(line)
    assert gs.archived(str(plain)) == str(plain) + ".gz"
    assert len(gs._hm_rebuilt(str(plain), "2026-10-26")) == 1
    assert len(gs._hm_snapshots(str(plain), "2026-10-26")) == 1


def _level_rows():
    et = lambda h, m: datetime(2026, 9, 17, h, m, tzinfo=ET)
    return et, pd.DataFrame({'et': [et(12, 0), et(12, 30), et(15, 0)], 'pin': [7000.0, None, 7100.0]})


def test_level_at_takes_the_last_value_in_force():
    et, df = _level_rows()
    assert gs._level_at(df, 'pin', et(12, 10)) == 7000.0
    assert gs._level_at(df, 'pin', et(15, 10)) == 7100.0


def test_level_at_blank_in_a_fresh_row_keeps_the_last_value():
    et, df = _level_rows()
    assert gs._level_at(df, 'pin', et(12, 40)) == 7000.0


def test_level_at_never_grades_across_a_gap_in_the_rows():
    et, df = _level_rows()
    assert gs._level_at(df, 'pin', et(14, 0)) is None
    assert gs._level_at(df, 'pin', et(15, 59)) is None


def test_level_at_never_looks_ahead_to_the_final_pin():
    et, df = _level_rows()
    assert gs._level_at(df, 'pin', et(11, 0)) is None


def test_level_at_ahead_only_reaches_a_row_just_after():
    et, df = _level_rows()
    assert gs._level_at(df, 'pin', et(11, 45), ahead=True) == 7000.0
    assert gs._level_at(df, 'pin', et(11, 0), ahead=True) is None
