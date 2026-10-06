"""
Rolling volume profile — 0DTE call/put volume traded per strike over the last N minutes.

How it works
------------
Schwab's chain gives `totalVolume` per contract = cumulative volume since the open.
Each GEX refresh we snapshot {strike: [call_vol, put_vol]} for 0DTE contracts.
Rolling volume for a window = latest snapshot − the snapshot taken `window` seconds ago.

Resolution is set by how often the chain is fetched. At a 30s GEX poll a "1m" window
really spans 60–90s; the API returns `covered_sec` so the UI shows the true span.

Persistence
-----------
Every snapshot is appended to data/rolling_profile_<SYM>_<YYYY-MM-DD>.jsonl.
On the first request after a restart, today's file is reloaded, so the 30m / 1h
windows survive a restart. Corrupt lines (e.g. a write cut off by a crash) are skipped.
The daily files double as a full-session record for later replay.

Wiring (dashboard.py)
---------------------
    from rolling_profile import rolling_bp, record_chain
    app.register_blueprint(rolling_bp)

    # in the GEX loop, right after the raw chain JSON is fetched:
    record_chain(api_symbol, chain)

Endpoint
--------
    GET /api/rolling_profile/SPX?window=300
"""

import json
import threading
import time
from collections import deque
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

try:
    ET = ZoneInfo("America/New_York")
except Exception as e:  # Windows has no system tz database
    raise ImportError("rolling_profile needs time zone data on Windows: pip install tzdata") from e
MAX_WINDOW_SEC = 3600            # longest selectable window (1h)
KEEP_SEC = MAX_WINDOW_SEC + 300  # keep a little extra so a full 1h base snapshot exists
ALLOWED_WINDOWS = {60, 120, 180, 300, 600, 900, 1800, 3600}
SESSION_OPEN = dtime(9, 30)      # snapshots outside this ET window are ignored: Schwab still
SESSION_CLOSE = dtime(16, 15)    # lists today's 0DTE after the close, which would evict the session

PERSIST = True
DATA_DIR = Path(__file__).resolve().parent.parent / "data"   # <project>/data
KEEP_DAYS = 30                   # day files older than this are deleted (~10 MB/day); None = keep all

STREAM_SNAPSHOT_SEC = 5          # snapshot cadence while streamed volume is arriving

_lock = threading.Lock()
_buffers: dict[str, deque] = {}  # sym -> deque[(ts, {strike: [call, put]}, spot)]
_loaded: set[str] = set()        # symbols whose day file has been read back

# Live per-contract cumulative 0DTE volume, fed by chain fetches (full book, every
# contract) and by streamer ticks (fast, one contract per strike/side). Snapshots
# are built from this, so both sources land in one consistent series.
_contracts: dict[str, dict[str, list]] = {}  # sym -> {contract: [strike, side_idx, vol]}
_contracts_day: dict[str, object] = {}       # sym -> ET date the contract state belongs to
_seeded: set[str] = set()                    # symbols with a chain fetch today (stream needs it)
_spot: dict[str, float] = {}                 # sym -> latest underlying price
_dirty: set[str] = set()                     # symbols with stream updates since last snapshot
_stream_thread = None


# ── helpers ──────────────────────────────────────────────────────────

def _norm(symbol: str) -> str:
    """'$SPX' / '/ES' / 'spx' -> 'SPX'"""
    return symbol.lstrip("$/").upper()


def _et_date(ts: float):
    return datetime.fromtimestamp(ts, ET).date()


def _in_session(ts: float) -> bool:
    t = datetime.fromtimestamp(ts, ET)
    return t.weekday() < 5 and SESSION_OPEN <= t.time() < SESSION_CLOSE


def _to_int(v) -> int:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return int(f) if f == f and f > 0 else 0       # NaN / negative -> 0


def _extract_0dte_volume(chain: dict) -> dict[float, list[int]]:
    """Sum 0DTE totalVolume per strike for calls and puts (SPX + SPXW both counted)."""
    out: dict[float, list[int]] = {}
    for side_idx, map_key in ((0, "callExpDateMap"), (1, "putExpDateMap")):
        for exp_key, strikes in (chain.get(map_key) or {}).items():
            if not str(exp_key).endswith(":0"):     # ":0" = expires today
                continue
            for strike_str, contracts in (strikes or {}).items():
                try:
                    strike = float(strike_str)
                except ValueError:
                    continue
                vol = sum(_to_int(c.get("totalVolume")) for c in (contracts or []))
                out.setdefault(strike, [0, 0])[side_idx] += vol
    return out


def _chain_contracts(chain: dict):
    """(contract_key, strike, side_idx, totalVolume) for every 0DTE contract in the chain."""
    for side_idx, map_key in ((0, "callExpDateMap"), (1, "putExpDateMap")):
        for exp_key, strikes in (chain.get(map_key) or {}).items():
            if not str(exp_key).endswith(":0"):
                continue
            for strike_str, contracts in (strikes or {}).items():
                try:
                    strike = float(strike_str)
                except ValueError:
                    continue
                for i, c in enumerate(contracts or []):
                    key = c.get("symbol") or f"{exp_key}|{strike_str}|{side_idx}|{i}"
                    yield key, strike, side_idx, _to_int(c.get("totalVolume"))


def _chain_spot(chain: dict):
    spot = chain.get("underlyingPrice")
    if not spot:
        spot = (chain.get("underlying") or {}).get("last")
    try:
        return float(spot) if spot else None
    except (TypeError, ValueError):
        return None


# ── persistence ──────────────────────────────────────────────────────

def _day_file(sym: str, ts: float) -> Path:
    return DATA_DIR / f"rolling_profile_{sym}_{_et_date(ts).isoformat()}.jsonl"


def _append_line(sym: str, ts: float, snap: dict, spot) -> None:
    if not PERSIST:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": ts, "spot": spot,
                           "v": {f"{k:g}": cp for k, cp in snap.items()}},
                          separators=(",", ":"))
        with open(_day_file(sym, ts), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[rolling_profile] persist failed: {e}")


def _cleanup_old(sym: str, now: float) -> None:
    if not PERSIST or KEEP_DAYS is None:
        return
    today = _et_date(now)
    for f in DATA_DIR.glob(f"rolling_profile_{sym}_*.jsonl"):
        try:
            d = datetime.strptime(f.stem.rsplit("_", 1)[1], "%Y-%m-%d").date()
            if (today - d).days > KEEP_DAYS:
                f.unlink()
        except (ValueError, OSError):
            continue


def _load_today(sym: str, now: float) -> deque:
    """Read back today's snapshots within KEEP_SEC. Skips corrupt/partial lines."""
    buf: deque = deque()
    if not PERSIST:
        return buf
    path = _day_file(sym, now)
    if not path.exists():
        return buf
    bad = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip().replace("\x00", "")
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                    ts = float(row["ts"])
                    snap = {float(k): [int(cp[0]), int(cp[1])] for k, cp in row["v"].items()}
                except (ValueError, KeyError, TypeError, IndexError):
                    bad += 1
                    continue
                if not buf or ts > buf[-1][0]:
                    buf.append((ts, snap, row.get("spot")))
    except OSError as e:
        print(f"[rolling_profile] reload failed: {e}")
    # keep the last KEEP_SEC of the session (relative to the last saved snapshot,
    # so an after-hours restart still shows the final hour of the day)
    while buf and buf[0][0] < buf[-1][0] - KEEP_SEC:
        buf.popleft()
    if buf or bad:
        print(f"[rolling_profile] {sym}: reloaded {len(buf)} snapshots"
              + (f", skipped {bad} bad lines" if bad else ""))
    return buf


def _ensure_loaded(sym: str, now: float) -> None:
    """Caller holds _lock."""
    if sym in _loaded:
        return
    _loaded.add(sym)
    _cleanup_old(sym, now)
    disk = _load_today(sym, now)
    mem = _buffers.get(sym)
    if mem:                      # keep anything recorded before the reload, newest wins
        cutoff = mem[0][0]
        disk = deque(r for r in disk if r[0] < cutoff)
        disk.extend(mem)
    _buffers[sym] = disk


# ── public API ───────────────────────────────────────────────────────

def record_chain(symbol: str, chain: dict, ts: float | None = None) -> None:
    """Snapshot 0DTE per-strike volume from a raw Schwab chain response."""
    if not isinstance(chain, dict) or not chain:
        return
    snap = _extract_0dte_volume(chain)
    if not snap:                                   # after hours / no 0DTE listed
        return
    ts = ts or time.time()
    if not _in_session(ts):
        return
    sym = _norm(symbol)
    spot = _chain_spot(chain)
    with _lock:
        _set_contracts_day(sym, ts)
        state = _contracts.setdefault(sym, {})
        for key, strike, side_idx, vol in _chain_contracts(chain):
            prev = state.get(key)
            # cumulative volume never falls; a chain response can be older than the stream
            state[key] = [strike, side_idx, max(vol, prev[2]) if prev else vol]
        _seeded.add(sym)
        if spot:
            _spot[sym] = spot
        _append_snapshot(sym, ts, _state_snapshot(sym), _spot.get(sym))


def record_stream_volume(symbol: str, contract: str, strike: float, side: str,
                         volume, ts: float | None = None) -> None:
    """Update one contract's cumulative volume from a streamer tick.

    Ignored until a chain fetch has seeded today's full book, so a snapshot never
    covers only the handful of contracts that happened to tick first.
    """
    ts = ts or time.time()
    vol = _to_int(volume)
    if vol <= 0 or not _in_session(ts):
        return
    sym = _norm(symbol)
    with _lock:
        _set_contracts_day(sym, ts)
        if sym not in _seeded:
            return
        state = _contracts[sym]
        prev = state.get(contract)
        if prev and vol <= prev[2]:
            return
        state[contract] = [float(strike), 0 if side == "call" else 1, vol]
        _dirty.add(sym)
    _ensure_stream_thread()


def update_spot(symbol: str, price) -> None:
    try:
        p = float(price)
    except (TypeError, ValueError):
        return
    if p > 0:
        _spot[_norm(symbol)] = p


def flush_stream(now: float | None = None) -> None:
    """Snapshot every symbol that received stream updates since the last flush."""
    now = now or time.time()
    with _lock:
        syms = list(_dirty)
        _dirty.clear()
        for sym in syms:
            _append_snapshot(sym, now, _state_snapshot(sym), _spot.get(sym))


def _stream_loop() -> None:
    while True:
        time.sleep(STREAM_SNAPSHOT_SEC)
        try:
            flush_stream()
        except Exception as e:  # never kill the thread
            print(f"[rolling_profile] stream snapshot failed: {e}")


def _ensure_stream_thread() -> None:
    global _stream_thread
    if _stream_thread is None:
        with _lock:
            if _stream_thread is None:
                _stream_thread = threading.Thread(target=_stream_loop, daemon=True,
                                                  name="rolling_profile_stream")
                _stream_thread.start()


def _set_contracts_day(sym: str, ts: float) -> None:
    """Caller holds _lock. Cumulative volume resets each session."""
    day = _et_date(ts)
    if _contracts_day.get(sym) != day:
        _contracts_day[sym] = day
        _contracts[sym] = {}
        _seeded.discard(sym)


def _state_snapshot(sym: str) -> dict[float, list[int]]:
    """Caller holds _lock. Per-strike call/put totals from the contract state."""
    snap: dict[float, list[int]] = {}
    for strike, side_idx, vol in _contracts.get(sym, {}).values():
        snap.setdefault(strike, [0, 0])[side_idx] += vol
    return snap


def _append_snapshot(sym: str, ts: float, snap: dict, spot) -> None:
    """Caller holds _lock."""
    if not snap:
        return
    _ensure_loaded(sym, ts)
    buf = _buffers.setdefault(sym, deque())
    if buf and _et_date(buf[-1][0]) != _et_date(ts):
        buf.clear()                                # new session — cumulative volume reset
    if buf and ts <= buf[-1][0]:
        return                                     # out-of-order / duplicate
    buf.append((ts, snap, spot))
    while buf and buf[0][0] < ts - KEEP_SEC:
        buf.popleft()
    _append_line(sym, ts, snap, spot)


def rolling(symbol: str, window_sec: int, now: float | None = None) -> dict:
    """Call/put volume per strike traded in the last `window_sec` seconds."""
    sym = _norm(symbol)
    with _lock:
        _ensure_loaded(sym, now or time.time())
        buf = list(_buffers.get(sym) or [])

    if len(buf) < 2:
        return {"symbol": sym, "window_sec": window_sec, "covered_sec": 0,
                "partial": True, "spot": buf[-1][2] if buf else None,
                "updated": None, "strikes": [], "call": [], "put": []}

    latest_ts, latest, spot = buf[-1]
    target = latest_ts - window_sec

    base = None
    for ts, snap, _ in reversed(buf[:-1]):        # newest snapshot at/before target
        if ts <= target:
            base = (ts, snap)
            break
    partial = base is None
    if partial:                                    # not enough history yet — use oldest
        base = (buf[0][0], buf[0][1])
    base_ts, base_snap = base

    strikes, call, put = [], [], []
    for k in sorted(latest):
        if k not in base_snap:                     # strike entered the chain mid-window:
            c = p = 0                              # unknown base, don't fake a spike
        else:
            c = max(0, latest[k][0] - base_snap[k][0])
            p = max(0, latest[k][1] - base_snap[k][1])
        strikes.append(k)
        call.append(c)
        put.append(p)

    return {
        "symbol": sym,
        "window_sec": window_sec,
        "covered_sec": int(latest_ts - base_ts),
        "partial": partial,
        "spot": spot,
        "updated": datetime.fromtimestamp(latest_ts, ET).strftime("%H:%M:%S"),
        "strikes": strikes,
        "call": call,
        "put": put,
    }


def _reset_for_tests() -> None:
    with _lock:
        _buffers.clear()
        _loaded.clear()
        _contracts.clear()
        _contracts_day.clear()
        _seeded.clear()
        _spot.clear()
        _dirty.clear()


rolling_bp = Blueprint("rolling_profile", __name__)


@rolling_bp.route("/api/rolling_profile/<symbol>")
def api_rolling_profile(symbol):
    try:
        window = int(request.args.get("window", 300))
    except ValueError:
        window = 300
    if window not in ALLOWED_WINDOWS:
        window = 300
    return jsonify(rolling(symbol, window))
