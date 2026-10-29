import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import predictions as P  # noqa: E402

DAY = "2026-10-21"
T0 = P._open_ts(DAY)


def bars_from(closes, start_min=0, wick=0.5):
    """1-min bars from a list of closes (open = previous close)."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append((T0 + 60 * (start_min + i), o, max(o, c) + wick, min(o, c) - wick, c))
        prev = c
    return out


def tl(strike, gex, names=("support",), t=T0 - 60, brakes=1.0):
    return [(t, {strike: list(names)}, {strike: gex, strike + 50: 1.0}, brakes)]


def test_level_hold_and_break():
    # from 110 down to the 100 level, then back up to 106: held
    closes = [110, 108, 105, 102, 100.5, 103, 106, 107]
    rows = P.predict_levels(DAY, bars_from(closes), [], tl(100.0, 5e6))
    lv = [r for r in rows if r["kind"] == "level"]
    assert len(lv) == 1 and lv[0]["from"] == "above" and lv[0]["predict"] == "hold"
    assert lv[0]["first"] == "hold" and lv[0]["min"] == 3, lv[0]
    assert P.grade(lv[0], 15) is True and P.grade(lv[0], 1) is None, "decided at minute 3"
    # a negative strike: down through it, a close 3 below = broke
    closes = [110, 106, 103, 100.5, 98, 96, 95]
    rows = P.predict_levels(DAY, bars_from(closes), [], tl(100.0, -5e6, ("trapdoor",)))
    lv = [r for r in rows if r["kind"] == "level"][0]
    assert lv["predict"] == "break" and lv["first"] == "break" and lv["names"] == ["trapdoor"]


def test_one_event_per_approach():
    # touches twice without getting 5 pts away in between: one event; away and back: a second
    closes = [110, 104, 100.5, 102, 100.8, 103, 108, 104, 100.6, 106]
    lv = [r for r in P.predict_levels(DAY, bars_from(closes), [], tl(100.0, 5e6)) if r["kind"] == "level"]
    assert len(lv) == 2, [(r["t"] - T0) // 60 for r in lv]


def test_label_memory_and_not_armed_at_the_level():
    # named only for the first minute: still a level 10 min later (support steps aside near price)
    closes = [110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100.5, 104, 106]
    lv = [r for r in P.predict_levels(DAY, bars_from(closes), [], tl(100.0, 5e6)) if r["kind"] == "level"]
    assert len(lv) == 1
    # a level that appears with price already on it isn't an approach
    closes = [100.5, 100.2, 100.8, 100.4]
    assert not P.predict_levels(DAY, bars_from(closes), [], tl(100.0, 5e6))


def test_run_after_break():
    # break of a negative strike at 97 (close 3 below 100), then 10 more down: ran
    closes = [110, 105, 101, 97, 94, 91, 87, 85]
    rows = P.predict_levels(DAY, bars_from(closes, wick=0), [], tl(100.0, -5e6, ("trapdoor",)))
    run = [r for r in rows if r["kind"] == "run"][0]
    assert run["dir"] == "down" and run["predict"] == "run" and run["first"] == "run", run
    # breaks, then closes back above 100: reclaimed
    closes = [110, 105, 101, 97, 99, 100.5, 102]
    rows = P.predict_levels(DAY, bars_from(closes, wick=0), [], tl(100.0, -5e6, ("trapdoor",)))
    assert [r for r in rows if r["kind"] == "run"][0]["first"] == "back"
    # a positive strike's break is recorded for comparison, with no prediction
    rows = P.predict_levels(DAY, bars_from(closes, wick=0), [], tl(100.0, 5e6))
    run = [r for r in rows if r["kind"] == "run"]
    assert run and run[0]["predict"] is None and P.status(run[0], T0 + 99999) == "comparison"


def test_size_vs_usual_and_no_gex_guess():
    # quiet 15 min, then a 20-pt range in the next 15: bigger than the usual 5
    closes = [100 + (i % 2) for i in range(15)] + [100 + 1.4 * i for i in range(15)] + [121] * 40
    bars = bars_from(closes, wick=0)
    base = {(m, h): 5.0 for h in P.HORIZONS for m in range(0, 391, 15)}
    snaps = [(T0 + 60 * 10, {"total_gex": "-3e8"})]
    rows = P.predict_size(DAY, bars, base, snaps, [], now=T0 + 86400)
    r = [x for x in rows if x["kind"] == "size_regime" and x["t"] == T0 + 900][0]
    assert r["predict"] == "bigger" and r["h"]["15"][0] > 5 and P.grade(r, 15) is True
    assert r["prev"][0] < r["prev"][1], "the 15 min before were quiet: the no-GEX guess says smaller"
    card = [c for c in P.summarize([dict(x, day=DAY) for x in rows]) if c["key"] == "size_regime"][0]
    col = card["cols"][0]
    assert col["n"] >= 1 and col["split"]["n"] >= 1, "GEX and the no-GEX guess disagreed at 9:45"
    # a snapshot older than STALE_SEC isn't in force
    assert P._at([(T0, {"total_gex": "1"})], T0 + P.STALE_SEC + 1, "total_gex") is None


def test_size_pending_today():
    bars = bars_from([100] * 20, wick=0)
    base = {(m, h): 5.0 for h in P.HORIZONS for m in range(0, 391, 15)}
    rows = P.predict_size(DAY, bars, base, [(T0, {"total_gex": "1e8"})], [], now=T0 + 60 * 20)
    assert rows and rows[0]["h"]["30"] is None and P.status(rows[0], T0 + 60 * 20) == "pending"


def test_pin():
    closes = [100.0] * 260 + [110.0] * 129 + [104.0]      # 14:00 at 110, closes at 104
    bars = bars_from(closes, wick=0)
    snaps = [(T0 + 60 * 260, {"true_pin": "100"})]
    snaps0 = [(T0 + 60 * 260, {"total_gex": "2e7", "pin": "105"})]
    rows = P.predict_pin(DAY, bars, snaps, snaps0, now=T0 + 86400)
    first = {r["kind"]: r for r in rows if r["t"] == T0 + 60 * 270}
    assert first["pin_true"]["nearer"] is True and first["pin_true"]["predict"] == "nearer"
    assert first["pin_0dte"]["nearer"] is True, "|104-105| < |110-105|"


def test_save_is_append_only(tmp_path, monkeypatch):
    for d in ("2026-10-20", "2026-10-21"):
        (tmp_path / f"gex_snapshots_SPX_{d}.csv").write_text("timestamp,spot,total_gex\n", encoding="utf-8")
    calls = []

    def fake_rows(day, by_day=None, now=None, data_dir=None):
        calls.append(day)
        if day == "2026-10-20":
            raise ValueError("no bars")
        return [{"kind": "level", "day": day, "t": 1, "gex": 1, "predict": "hold", "first": "hold", "min": 2}]
    monkeypatch.setattr(P, "day_rows", fake_rows)
    monkeypatch.setattr(P, "sessions", lambda max_age=60: {})
    out = tmp_path / "pred.jsonl"
    assert P.save_finished_days(str(tmp_path), str(out), today="2026-10-27") == 1
    assert P.save_finished_days(str(tmp_path), str(out), today="2026-10-27") == 0, "nothing redone"
    assert calls == ["2026-10-20", "2026-10-21"], "the failed day got a marker, not a retry"
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert {(r["day"], r["kind"]) for r in lines} == {("2026-10-20", "none"), ("2026-10-21", "level")}
    # a rules change saves the days again, beside the old rows
    monkeypatch.setattr(P, "RULES_VERSION", P.RULES_VERSION + 1)
    P.save_finished_days(str(tmp_path), str(out), today="2026-10-27")
    assert len(out.read_text(encoding="utf-8").splitlines()) == 4


# ── touch trades ──────────────────────────────────────────────────────────────

def _bars_csv(path, rows):
    head = "time,symbol,expiry,strike,side,mid_open,mid_high,mid_low,mid_close,bid,ask,last,volume,quotes,iv,delta,gamma,oi,spot\n"
    with open(path, "a", encoding="utf-8") as f:
        if f.tell() == 0:
            f.write(head)
        for t, exp, k, side, bid, ask in rows:
            f.write(f"{t},SPXW  X,{exp},{k},{side},0,0,0,0,{bid},{ask},0,0,1,0.2,0.5,0.01,10,100\n")


def test_option_quotes_reads_todays_file_as_it_grows(tmp_path):
    p = tmp_path / f"option_bars_SPX_{DAY}.csv"
    _bars_csv(p, [(T0, DAY, 100.0, "call", 2.0, 2.1), (T0, "2026-10-22", 100.0, "call", 5.0, 5.2),   # not 0DTE
                  (T0 + 60, DAY, 100.0, "put", 0.0, 0.05)])                                          # no bid
    q = P.option_quotes(DAY, data_dir=tmp_path, today=DAY)
    assert q == {("call", 100.0): {T0: (2.0, 2.1)}}
    _bars_csv(p, [(T0 + 60, DAY, 100.0, "call", 2.2, 2.3)])
    assert P.option_quotes(DAY, data_dir=tmp_path, today=DAY)[("call", 100.0)] == {T0: (2.0, 2.1), T0 + 60: (2.2, 2.3)}
    assert P.option_quotes(DAY, data_dir=tmp_path, today="2026-10-30") == q | {("call", 100.0): {T0: (2.0, 2.1), T0 + 60: (2.2, 2.3)}}, \
        "a finished day: read whole"


def test_touch_trade_bought_at_the_ask_sold_at_the_bid():
    # gold at 100: down from 116, within 5 pts at 104.5 (minute 15), then up 6.5 pts
    closes = [116] * 12 + [113, 111, 109, 104.5, 106, 108, 111] + [112] * 60
    bars = bars_from(closes)
    t = T0 + 60 * 15
    q = {("call", 105.0): {t: (2.0, 2.1), t + 900: (2.5, 2.6), t + 1800: (3.0, 3.1), t + 3600: (1.0, 1.1)}}
    rows = P.predict_trades(DAY, bars, {"gold": [100.0] * len(bars)}, q, now=T0 + 36000)
    touch = [r for r in rows if r["level"] == "gold"]
    assert len(touch) == 1 and touch[0]["from"] == "above" and touch[0]["predict"] == "bounce"
    assert touch[0]["k"] == 105 and touch[0]["move"] == "bounce", "the option nearest the money; up 5 first"
    b = touch[0]["bounce"]
    assert b["side"] == "call" and b["ask"] == 2.1
    assert (b["15"], b["30"], b["60"]) == (19.0, 42.9, -52.4) and b["hit"] is True
    assert b["tp"] == 42.9, "sold the first minute the bid was 25% up"
    assert touch[0]["break"] is None, "no put quotes"
    base = [r for r in rows if r["level"] == "none"]
    assert [((r["t"] - T0) // 60) for r in base] == [15], "every 15 min, where there's a quote"
    assert P.status(dict(touch[0]), now=T0 + 36000) == "hit"
    pending = P.predict_trades(DAY, bars, {"gold": [100.0] * len(bars)}, q, now=t + 1200)
    g = [r for r in pending if r["level"] == "gold"][0]
    assert g["bounce"]["15"] == 19.0 and g["bounce"]["30"] is None and g["bounce"]["hit"] is None and g["bounce"]["tp"] is None
    assert P.status(dict(g), now=t + 1200) == "pending"
    card = next(c for c in P.summarize([dict(r, day=DAY) for r in rows]) if c["key"] == "trade_gold")
    assert card["cols"][1]["rate"] == 43 and card["cols"][1]["base"] == 43 and card["cols"][1]["pnl"]
    assert "5 pts its way first 100% (n 1)" in card["detail"] and "take profit at +25% (else out at 60 min) +43% (n 1)" in card["detail"]


def test_touch_trade_levels_open_and_previous_session():
    prev = bars_from([90, 95, 99])
    today = bars_from([101, 102])
    L = P.trade_levels(DAY, today, {"2026-10-20": prev, DAY: today}, {}, [], [], [])
    assert L["open"] == [101.0, 101.0], "today's first price"
    assert L["prev open"] == [90.0, 90.0] and L["prev close"] == [99.0, 99.0]
    no_prev = P.trade_levels(DAY, today, {DAY: today}, {}, [], [], [])
    assert no_prev["prev close"] == [None, None]


def test_size_predictions_every_7_5_minutes():
    ms = P._check_times(15)
    assert ms[:6] == [15, 23, 30, 38, 45, 53], "9:45, 9:53, 10:00, 10:08, ..."
    assert ms[-1] == 375 and (ms[-1] - ms[0]) / (len(ms) - 1) == 7.5
    bars = bars_from([100 + (i % 3) for i in range(80)], wick=0)
    base = {(m, h): 5.0 for h in P.HORIZONS for m in range(0, 391)}
    snaps = [(T0 + 60 * i, {"total_gex": "1e8"}) for i in range(80)]      # one a minute: none goes stale
    rows = P.predict_size(DAY, bars, base, snaps, [], now=T0 + 86400)
    times = sorted({(r["t"] - T0) // 60 for r in rows if r["kind"] == "size_regime"})
    assert times[:4] == [15, 23, 30, 38]
    assert all(r["prev"] for r in rows if r["kind"] == "size_regime"), "the no-GEX guess at every one (the 15 min before)"


def test_charm_drift(tmp_path):
    # 14:00: a big 0DTE put block below price (dealers short it, hedged with short futures):
    # as it decays they buy those back, flow BUY at price → closes higher; it did
    import bs
    P._charm_cache.clear()
    spot, k, sig = 7712.0, 7690.0, 0.15
    T = 2 * 3600 / (365 * 24 * 3600)
    lines = ["timestamp,symbol,strike,side,expiry_label,delta,oi"]
    for side in ("put", "call"):
        d = bs.delta(spot, k, T, 0.05, sig, side[0])
        oi = 20000 if side == "put" else 0     # (equal call OI would cancel it: long call + short put has no charm)
        lines.append(f"2026-10-21 10:59:40,SPXW  261021{side[0].upper()}0{int(k)}000,{k},{side},0DTE,{d:.4f},{oi}")
    (tmp_path / f"gex_watchlist_SPX_{DAY}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    closes = [spot] * 300 + [spot + 8.0 * i / 89 for i in range(1, 91)]     # 15:00 on, up 8 pts by the close
    rows = P.predict_charm(DAY, bars_from(closes, wick=0), now=T0 + 86400, data_dir=str(tmp_path))
    first = next(r for r in rows if r["t"] == T0 + 60 * 270)
    assert first["flow"] > 0 and first["predict"] == "up" and first["up"] is True
    assert P.grade(first) is True and P.status(first, T0 + 86400) == "hit"
    assert [r["t"] for r in rows] == [T0 + 60 * 270], "the later checkpoints have no fresh snapshot"
    card = next(c for c in P.summarize([dict(r, day=DAY) for r in rows]) if c["key"] == "charm")
    assert card["cols"][0]["hits"] == 1 and card["cols"][0]["n"] == 1
