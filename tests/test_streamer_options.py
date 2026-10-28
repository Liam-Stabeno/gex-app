"""streamer: LEVELONE_OPTIONS parsing and the fill-from-memory before on_options_quote."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import streamer  # noqa: E402

SYM = "SPXW  261028C07735000"


def test_fields_subscribed_and_parsed():
    assert set(streamer.OPTIONS_FIELDS.split(",")) >= {"16", "17", "18"}, "bid / ask / last size"
    q = streamer._parse_options({"content": [{"key": SYM, "2": 22.1, "3": 22.2, "4": 22.2, "8": 2313,
                                              "16": 12, "17": 30.0, "18": 4}]})[0]
    assert (q["bid_size"], q["ask_size"], q["last_size"]) == (12, 30, 4)
    q = streamer._parse_options({"content": [{"key": SYM, "2": 22.0}]})[0]
    assert q["last_size"] is None and q["volume"] is None, "a field Schwab didn't resend"


def test_sizes_filled_from_memory():
    seen = []
    s = streamer.SchwabStreamer(on_candle=lambda *a: None, on_options_quote=seen.append)
    s._contract_info[SYM] = {"strike": 7735.0, "side": "call", "expiry_label": "0DTE", "weight": 0.5, "is_0dte": True}
    for raw in ({"key": SYM, "2": 22.1, "3": 22.2, "4": 22.2, "8": 2313, "16": 12, "17": 30, "18": 4},
                {"key": SYM, "2": 22.0, "3": 22.1}):                           # quote only: sizes unchanged
        s._handle_options_quote(streamer._parse_options({"content": [raw]})[0])
    assert seen[-1]["bid"] == 22.0 and (seen[-1]["bid_size"], seen[-1]["last_size"], seen[-1]["volume"]) == (12, 4, 2313)


def test_alert_side_from_the_quote_before_the_trade():
    alerts = []
    s = streamer.SchwabStreamer(on_candle=lambda *a: None, on_flow_alert=alerts.append)
    s._contract_info[SYM] = {"strike": 7735.0, "side": "call", "expiry_label": "0DTE", "weight": 1.0, "is_0dte": True}

    def tick(**f):
        s._handle_options_quote(streamer._parse_options({"content": [{"key": SYM, **f}]})[0])
    tick(**{"2": 1.0, "3": 1.2, "4": 1.1, "8": 100})
    # 1,000 traded at 1.2 = the ask that stood; the quote on the same tick already moved up to 1.3/1.5
    tick(**{"2": 1.3, "3": 1.5, "4": 1.2, "8": 1100})
    assert alerts[-1]["direction"] == "BUY" and alerts[-1]["direction_certain"], alerts[-1]
    assert (alerts[-1]["bid"], alerts[-1]["ask"]) == (1.0, 1.2), "the quote it was judged on"
    # far outside the quote before (1.3/1.5, spread 0.2): a multi-leg order's leg, no side
    tick(**{"4": 2.4, "8": 2100})
    assert alerts[-1]["direction"] is None
