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
