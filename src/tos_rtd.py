"""
ThinkorSwim RTD (Real-Time Data) COM client.

Architecture
------------
- tos.rtd is InprocServer32 (RTDService.dll inside TOS install)
- Dispatch("tos.rtd") loads the DLL into our process — works without Excel
- ServerStart() requires an IRTDUpdateEvent COM callback; we try multiple
  strategies to satisfy it (or skip it if ConnectData works without it)
- ConnectData() subscribes to BID + ASK for each symbol
- RefreshData() polled every ~100 ms
- Thread-safe _quotes dict readable from any thread via get_quote()

Connection strategy (tried in order):
  1. Dispatch + ConnectData directly (no ServerStart)  — simplest
  2. Dispatch + ServerStart via ctypes minimal vtable  — if (1) fails
  3. GetActiveObject                                   — if Excel/TOS setting primed it

Option symbol format: .SPX260620C5400
"""

import threading
import time
import winreg
import ctypes
import pythoncom
import win32com.client


# ── Minimal IRTDUpdateEvent vtable via ctypes ─────────────────────────────────
# ServerStart requires a real COM interface pointer — no way around it.
# We build the smallest possible vtable: QI / AddRef / Release / UpdateNotify
# HeartbeatInterval get/set.  All stubs; we poll via RefreshData instead.

_WINFUNCTYPE = ctypes.WINFUNCTYPE

def _qi(this, riid, ppv):
    ppv_ptr = ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))
    ppv_ptr[0] = this
    return 0   # S_OK

def _addref(this):   return 1
def _release(this):  return 1
def _update_notify(this): return 0   # S_OK
def _hb_get(this, p):
    ctypes.cast(p, ctypes.POINTER(ctypes.c_long))[0] = -1   # disable heartbeat
    return 0
def _hb_set(this, v): return 0

_QI_FUNC       = _WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_ULONG_FUNC    = _WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_HRESULT_FUNC  = _WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
_HB_GET_FUNC   = _WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
_HB_SET_FUNC   = _WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_long)

class _RTDCallbackVtable(ctypes.Structure):
    _fields_ = [
        ('QueryInterface',    _QI_FUNC),
        ('AddRef',            _ULONG_FUNC),
        ('Release',           _ULONG_FUNC),
        ('GetTypeInfoCount',  _HRESULT_FUNC),
        ('GetTypeInfo',       _HRESULT_FUNC),
        ('GetIDsOfNames',     _HRESULT_FUNC),
        ('Invoke',            _HRESULT_FUNC),
        ('UpdateNotify',      _HRESULT_FUNC),
        ('HeartbeatInterval_get', _HB_GET_FUNC),
        ('HeartbeatInterval_set', _HB_SET_FUNC),
    ]

def _noop(this): return 0

_vtable = _RTDCallbackVtable(
    _QI_FUNC(_qi),
    _ULONG_FUNC(_addref),
    _ULONG_FUNC(_release),
    _HRESULT_FUNC(_noop),   # GetTypeInfoCount
    _HRESULT_FUNC(_noop),   # GetTypeInfo
    _HRESULT_FUNC(_noop),   # GetIDsOfNames
    _HRESULT_FUNC(_noop),   # Invoke
    _HRESULT_FUNC(_update_notify),
    _HB_GET_FUNC(_hb_get),
    _HB_SET_FUNC(_hb_set),
)

class _RTDCallbackStruct(ctypes.Structure):
    _fields_ = [('lpVtbl', ctypes.POINTER(_RTDCallbackVtable))]

_callback_struct = _RTDCallbackStruct(ctypes.pointer(_vtable))
_callback_ptr    = ctypes.byref(_callback_struct)


# ── Public quote store ────────────────────────────────────────────────────────

_quotes: dict[str, dict] = {}
_quotes_lock = threading.Lock()

# ── Internal state ────────────────────────────────────────────────────────────

_thread: threading.Thread | None = None
_running   = False
_pending_symbols: set[str] = set()
_pending_lock    = threading.Lock()
_available: bool | None = None

# ── Toggle state ──────────────────────────────────────────────────────────────
_enabled     = True
_connected   = False
_quote_count = 0


# ── Symbol conversion ─────────────────────────────────────────────────────────

def _tos_symbol(expiry_date: str, side: str, strike: float,
                underlying: str = 'SPX') -> str:
    y, m, d = expiry_date.split('-')
    yymmdd  = y[2:] + m + d
    cp      = 'C' if side == 'call' else 'P'
    k       = int(strike) if strike == int(strike) else strike
    return f'.{underlying}{yymmdd}{cp}{k}'


def contract_to_tos(contract: dict) -> str:
    occ        = contract.get('symbol', '')
    underlying = occ[:6].strip() if occ else contract.get('underlying', 'SPX')
    return _tos_symbol(
        expiry_date=contract['expiry_date'],
        side=contract['side'],
        strike=contract['strike'],
        underlying=underlying,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def is_available() -> bool:
    global _available
    if _available is not None:
        return _available
    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, 'tos.rtd')
        _available = True
    except (FileNotFoundError, OSError):
        _available = False
    return _available


def is_enabled() -> bool:   return _enabled
def get_quote(tos_symbol: str) -> tuple[float, float]:
    with _quotes_lock:
        q = _quotes.get(tos_symbol, {})
        return q.get('bid', 0.0), q.get('ask', 0.0)

def toggle() -> dict:
    global _enabled
    _enabled = not _enabled
    print(f'[RTD] {"Enabled" if _enabled else "Disabled"} by user')
    return get_status()

def get_status() -> dict:
    return {'registered': is_available(), 'connected': _connected,
            'enabled': _enabled, 'quote_count': _quote_count}

def add_symbols(tos_symbols: list[str]):
    with _pending_lock:
        for s in tos_symbols:
            if s not in _quotes:
                _pending_symbols.add(s)

def start():
    global _thread, _running
    if not is_available():
        print('[RTD] tos.rtd not in registry')
        return
    if _thread and _thread.is_alive():
        return
    _running = True
    _thread = threading.Thread(target=_rtd_thread_main, name='tos-rtd', daemon=True)
    _thread.start()
    print('[RTD] Thread started')

def stop():
    global _running
    _running = False


# ── COM thread ────────────────────────────────────────────────────────────────

def _try_connect():
    """Try every known strategy to get an RTD handle. Returns COM object or None."""

    # Strategy 1: Dispatch + ConnectData directly (no ServerStart)
    try:
        rtd = win32com.client.Dispatch('tos.rtd')
        # Probe with a simple symbol to see if data flows without ServerStart
        test_val = rtd.ConnectData(99999, ['LAST', '$SPX'], True)
        print(f'[RTD] Strategy 1 (Dispatch, no ServerStart) probe={test_val}')
        # If we get here without exception, it works
        rtd.DisconnectData(99999)
        return rtd
    except Exception as e:
        print(f'[RTD] Strategy 1 failed: {e}')

    # Strategy 2: Dispatch + ServerStart via ctypes vtable callback
    try:
        rtd = win32com.client.Dispatch('tos.rtd')
        # Pass our ctypes COM pointer as a raw integer — pywin32 accepts c_void_p via VARIANT
        cb_addr = ctypes.addressof(_callback_struct)
        result  = rtd.ServerStart(cb_addr)
        print(f'[RTD] Strategy 2 (Dispatch + ctypes callback) ServerStart={result}')
        if result == 1:
            return rtd
    except Exception as e:
        print(f'[RTD] Strategy 2 failed: {e}')

    # Strategy 3: GetActiveObject (Excel or TOS setting primed it)
    try:
        rtd = win32com.client.GetActiveObject('tos.rtd')
        print('[RTD] Strategy 3 (GetActiveObject) succeeded')
        return rtd
    except Exception as e:
        print(f'[RTD] Strategy 3 (GetActiveObject) failed: {e}')

    return None


def _rtd_thread_main():
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    global _connected, _running

    rtd = None
    while _running and rtd is None:
        rtd = _try_connect()
        if rtd:
            _connected = True
            print('[RTD] Connected — starting quote polling')
        else:
            _connected = False
            print('[RTD] All strategies failed — retrying in 15 s')
            for _ in range(150):
                if not _running: break
                pythoncom.PumpWaitingMessages()
                time.sleep(0.1)

    if rtd is None:
        pythoncom.CoUninitialize()
        return

    topic_id  = 1
    topic_map: dict[int, tuple[str, str]] = {}
    subscribed: set[str] = set()

    def _subscribe_batch(symbols):
        nonlocal topic_id
        for sym in symbols:
            if sym in subscribed:
                continue
            for field, fname in [('bid', 'BID'), ('ask', 'ASK')]:
                try:
                    val = rtd.ConnectData(topic_id, [fname, sym], True)
                    topic_map[topic_id] = (sym, field)
                    topic_id += 1
                    if val is not None:
                        try:
                            fval = float(val)
                            if fval > 0:
                                with _quotes_lock:
                                    _quotes.setdefault(sym, {})[field] = fval
                        except (TypeError, ValueError):
                            pass
                except Exception as e:
                    print(f'[RTD] ConnectData {sym}/{fname}: {e}')
            subscribed.add(sym)

    while _running:
        pythoncom.PumpWaitingMessages()

        with _pending_lock:
            new_syms = list(_pending_symbols - subscribed)
            _pending_symbols.clear()
        if new_syms:
            _subscribe_batch(new_syms)

        if not topic_map:
            time.sleep(0.1)
            continue

        try:
            topic_count = [0]
            updates = rtd.RefreshData(topic_count)
            if updates and len(updates) >= 2:
                ids, values = updates[0], updates[1]
                with _quotes_lock:
                    for i in range(len(ids)):
                        tid = ids[i]
                        val = values[i]
                        if tid not in topic_map or val is None:
                            continue
                        sym, field = topic_map[tid]
                        try:
                            fval = float(val)
                            if fval > 0:
                                _quotes.setdefault(sym, {})[field] = fval
                        except (TypeError, ValueError):
                            pass
                    global _quote_count
                    _quote_count = sum(1 for q in _quotes.values() if q)
        except Exception as e:
            print(f'[RTD] RefreshData error: {e}')
            _connected = False
            break

        time.sleep(0.1)

    _connected = False
    try:
        rtd.ServerTerminate()
    except Exception:
        pass
    pythoncom.CoUninitialize()
    print('[RTD] Thread stopped')
