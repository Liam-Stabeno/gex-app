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
