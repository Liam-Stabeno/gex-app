"""
Run from the project root:   python -m pytest tests/test_rolling_profile.py -q
"""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import rolling_profile as rp  # noqa: E402

T0 = 1791208800.0          # 2026-10-05 10:00:00 ET
EXP0 = "2026-10-05:0"


def chain(vols, spot=7775.0, exp=EXP0, extra_multi=True):
    """Minimal Schwab-shaped chain. vols = {strike: (call_vol, put_vol)}"""
    cm, pm = {exp: {}}, {exp: {}}
    for k, (c, p) in vols.items():
        cm[exp][f"{k:.1f}"] = [{"putCall": "CALL", "totalVolume": c}]
        pm[exp][f"{k:.1f}"] = [{"putCall": "PUT", "totalVolume": p}]
    if extra_multi:  # next-day expiry must be ignored
        cm["2026-10-06:1"] = {"7780.0": [{"totalVolume": 999_999}]}
        pm["2026-10-06:1"] = {"7780.0": [{"totalVolume": 999_999}]}
    return {"symbol": "$SPX", "underlyingPrice": spot,
            "callExpDateMap": cm, "putExpDateMap": pm}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rp, "PERSIST", True)
    rp._reset_for_tests()
    yield tmp_path
    rp._reset_for_tests()


def feed(n=31, step=30, t0=T0):
    """n snapshots every `step`s. 7780 calls +100/snap, 7750 puts +50/snap."""
    for i in range(n):
        v = {7750.0: (10 * i, 50 * i), 7780.0: (100 * i, 5 * i)}
        rp.record_chain("$SPX", chain(v), ts=t0 + step * i)


def at(r, strike, side):
    return r[side][r["strikes"].index(strike)]


# ── core math ────────────────────────────────────────────────────────

def test_window_deltas():
    feed()
    r = rp.rolling("SPX", 300, now=T0 + 900)
    assert r["covered_sec"] == 300 and not r["partial"]
    assert at(r, 7780.0, "call") == 1000
    assert at(r, 7750.0, "put") == 500


@pytest.mark.parametrize("w", sorted(rp.ALLOWED_WINDOWS))
def test_every_window(w):
    feed(n=int(3900 / 15) + 1, step=15)          # 65 min of 15s snapshots
    r = rp.rolling("SPX", w, now=T0 + 3900)
    assert r["covered_sec"] == w and not r["partial"]
    assert at(r, 7780.0, "call") == 100 * (w // 15)


def test_partial_when_history_short():
    feed(n=11)                                     # 5 min of history
    r = rp.rolling("SPX", 1800, now=T0 + 300)
    assert r["partial"] and r["covered_sec"] == 300


def test_coarse_poll_covers_more_than_window():
    feed(n=11, step=45)
    r = rp.rolling("SPX", 60, now=T0 + 450)
    assert r["covered_sec"] == 90                  # 45s cadence → 1m window spans 90s


def test_ignores_multi_expiry():
    feed(n=3)
    r = rp.rolling("SPX", 60, now=T0 + 60)
    assert max(r["call"]) < 999_999


def test_new_strike_mid_window_no_fake_spike():
    feed(n=20)
    for i in range(20, 31):
        v = {7750.0: (10 * i, 50 * i), 7780.0: (100 * i, 5 * i), 7790.0: (5000 + i, 0)}
        rp.record_chain("$SPX", chain(v), ts=T0 + 30 * i)
    assert at(rp.rolling("SPX", 600, now=T0 + 900), 7790.0, "call") == 0
    assert at(rp.rolling("SPX", 120, now=T0 + 900), 7790.0, "call") == 4


def test_negative_delta_clamped():
    rp.record_chain("$SPX", chain({7780.0: (500, 500)}), ts=T0)
    rp.record_chain("$SPX", chain({7780.0: (400, 450)}), ts=T0 + 60)
    r = rp.rolling("SPX", 60, now=T0 + 60)
    assert at(r, 7780.0, "call") == 0 and at(r, 7780.0, "put") == 0


def test_spxw_and_spx_summed_same_strike():
    c = chain({7780.0: (100, 0)})
    c["callExpDateMap"][EXP0]["7780.0"].append({"putCall": "CALL", "totalVolume": 50})
    rp.record_chain("$SPX", chain({7780.0: (0, 0)}), ts=T0)
    rp.record_chain("$SPX", c, ts=T0 + 60)
    assert at(rp.rolling("SPX", 60, now=T0 + 60), 7780.0, "call") == 150


def test_bad_values_tolerated():
    c = chain({7780.0: (0, 0)})
    c["callExpDateMap"][EXP0]["7780.0"] = [{"totalVolume": "NaN"}, {"totalVolume": None}, {}]
    c["callExpDateMap"][EXP0]["oops"] = [{"totalVolume": 5}]
    rp.record_chain("$SPX", c, ts=T0)
    rp.record_chain("$SPX", chain({7780.0: (10, 0)}), ts=T0 + 60)
    assert at(rp.rolling("SPX", 60, now=T0 + 60), 7780.0, "call") == 10


# ── session handling ─────────────────────────────────────────────────

def test_after_hours_chain_ignored():
    feed(n=5)
    rp.record_chain("$SPX", chain({7780.0: (1, 1)}, exp="2026-10-06:1", extra_multi=False), ts=T0 + 200)
    assert rp.rolling("SPX", 60, now=T0 + 200)["updated"] == "10:02:00"


def test_new_session_clears():
    feed(n=5)
    rp.record_chain("$SPX", chain({7780.0: (5, 5)}, exp="2026-10-06:0"), ts=T0 + 86400)
    assert rp.rolling("SPX", 60, now=T0 + 86400)["strikes"] == []


def test_out_of_order_dropped():
    feed(n=5)
    rp.record_chain("$SPX", chain({7780.0: (0, 0)}), ts=T0 + 30)
    assert rp.rolling("SPX", 60, now=T0 + 120)["updated"] == "10:02:00"


def test_empty():
    r = rp.rolling("SPX", 300, now=T0)
    assert r["strikes"] == [] and r["partial"] and r["spot"] is None


def test_symbol_normalisation():
    feed(n=3)
    assert rp.rolling("spx", 60, now=T0 + 60)["strikes"]
    assert rp.rolling("$SPX", 60, now=T0 + 60)["strikes"]


# ── persistence ──────────────────────────────────────────────────────

def test_survives_restart(isolated):
    feed(n=121, step=15)                           # 30 min
    rp._reset_for_tests()                          # simulate restart
    r = rp.rolling("SPX", 1800, now=T0 + 1800)
    assert not r["partial"] and at(r, 7780.0, "call") == 100 * 120


def test_after_hours_restart_shows_last_hour(isolated):
    feed(n=121, step=15)
    rp._reset_for_tests()
    r = rp.rolling("SPX", 1800, now=T0 + 6 * 3600)  # restart at 16:00 ET
    assert r["strikes"] and not r["partial"]


def test_corrupt_lines_skipped(isolated):
    feed(n=11)
    f = next(isolated.glob("rolling_profile_SPX_*.jsonl"))
    with open(f, "a", encoding="utf-8") as fh:
        fh.write('{"ts": 17912')                    # truncated write
        fh.write("\x00\x00\x00\n")                  # null bytes
        fh.write("not json\n")
    rp._reset_for_tests()
    r = rp.rolling("SPX", 300, now=T0 + 300)
    assert r["covered_sec"] == 300 and at(r, 7780.0, "call") == 1000


def test_record_after_restart_merges(isolated):
    feed(n=11)
    rp._reset_for_tests()
    rp.record_chain("$SPX", chain({7750.0: (110, 550), 7780.0: (1100, 55)}), ts=T0 + 330)
    r = rp.rolling("SPX", 330, now=T0 + 330)
    assert r["covered_sec"] == 330 and at(r, 7780.0, "call") == 1100


def test_file_is_valid_jsonl(isolated):
    feed(n=4)
    lines = next(isolated.glob("*.jsonl")).read_text().splitlines()
    assert len(lines) == 4 and all("v" in json.loads(l) for l in lines)


def test_persist_off(isolated, monkeypatch):
    monkeypatch.setattr(rp, "PERSIST", False)
    feed(n=4)
    assert not list(isolated.glob("*.jsonl"))


# ── concurrency ──────────────────────────────────────────────────────

def test_concurrent_read_write():
    errors = []

    def writer():
        try:
            for i in range(300):
                rp.record_chain("$SPX", chain({7780.0: (i, i)}), ts=T0 + i)
        except Exception as e:      # pragma: no cover
            errors.append(e)

    def reader():
        try:
            for _ in range(300):
                rp.rolling("SPX", 60, now=T0 + 300)
        except Exception as e:      # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert at(rp.rolling("SPX", 60, now=T0 + 299), 7780.0, "call") == 60


# ── HTTP ─────────────────────────────────────────────────────────────

def test_endpoint():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(rp.rolling_bp)
    c = app.test_client()
    feed()
    j = c.get("/api/rolling_profile/SPX?window=120").get_json()
    assert j["window_sec"] == 120 and j["strikes"]
    assert c.get("/api/rolling_profile/SPX?window=7").get_json()["window_sec"] == 300
    assert c.get("/api/rolling_profile/SPX?window=abc").status_code == 200


def test_old_day_files_cleaned(isolated, monkeypatch):
    monkeypatch.setattr(rp, "KEEP_DAYS", 30)
    old = isolated / "rolling_profile_SPX_2026-08-01.jsonl"
    recent = isolated / "rolling_profile_SPX_2026-09-20.jsonl"
    other = isolated / "rolling_profile_QQQ_2026-08-01.jsonl"
    for f in (old, recent, other):
        f.write_text("")
    feed(n=2)
    assert not old.exists() and recent.exists() and other.exists()


def test_post_close_snapshot_ignored():
    rp.record_chain("$SPX", chain({7780.0: (0, 0)}), ts=T0)
    rp.record_chain("$SPX", chain({7780.0: (100, 50)}), ts=T0 + 300)
    rp.record_chain("$SPX", chain({7780.0: (100, 50)}), ts=T0 + 10 * 3600)   # 20:00 ET, still lists 0DTE
    r = rp.rolling("$SPX", 300)
    assert r["updated"] == "10:05:00"
    assert r["call"] == [100] and r["put"] == [50]


# ── streamed volume ──────────────────────────────────────────────────

def test_stream_ignored_until_chain_seeds():
    rp.record_stream_volume("SPX", "C7780", 7780.0, "call", 500, ts=T0)
    rp.flush_stream(now=T0 + 5)
    assert rp.rolling("$SPX", 60)["strikes"] == []


def test_stream_ticks_give_true_short_windows():
    rp.record_chain("$SPX", chain({7780.0: (100, 0), 7750.0: (0, 40)}), ts=T0)
    for i in range(1, 25):                         # 2 min of ticks, snapshot every 5 s
        rp.record_stream_volume("SPX", "2026-10-05:0|7780.0|0|0", 7780.0, "call", 100 + 10 * i,
                                ts=T0 + 5 * i)
        rp.flush_stream(now=T0 + 5 * i)
    r = rp.rolling("$SPX", 60)
    assert r["covered_sec"] == 60 and not r["partial"]
    assert at(r, 7780.0, "call") == 120             # 12 snapshots × 10
    assert at(r, 7750.0, "put") == 0                 # unticked strike still present, flat


def test_stale_chain_does_not_roll_stream_back():
    rp.record_chain("$SPX", chain({7780.0: (100, 0)}), ts=T0)
    rp.record_stream_volume("SPX", "2026-10-05:0|7780.0|0|0", 7780.0, "call", 300, ts=T0 + 5)
    rp.flush_stream(now=T0 + 5)
    rp.record_chain("$SPX", chain({7780.0: (250, 0)}), ts=T0 + 10)   # older than the stream
    r = rp.rolling("$SPX", 60)
    assert at(r, 7780.0, "call") == 200


def test_stream_after_close_ignored():
    rp.record_chain("$SPX", chain({7780.0: (0, 0)}), ts=T0 - 60)
    rp.record_chain("$SPX", chain({7780.0: (100, 0)}), ts=T0)
    rp.record_stream_volume("SPX", "2026-10-05:0|7780.0|0|0", 7780.0, "call", 900, ts=T0 + 10 * 3600)
    rp.flush_stream(now=T0 + 10 * 3600)
    assert rp.rolling("$SPX", 60)["updated"] == "10:00:00"
