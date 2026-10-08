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
