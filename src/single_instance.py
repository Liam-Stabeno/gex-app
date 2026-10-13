"""
single_instance.py — only one copy of the dashboard may run at a time.

Two copies (the hidden 6:14 scheduled run plus a start.bat run) doubled every saved
row and knocked each other off Schwab's stream, which allows one connection per
account, on 2026-10-12. A named mutex (per Windows session) marks the running copy;
it is released when that process ends, however it ends.
"""
import ctypes

_handle = None   # kept for the life of the process


def acquire(name: str = 'Local\\GEXDashboard') -> bool:
    """True if this process may run (no other copy holds `name`), else False."""
    global _handle
    try:
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        h = k.CreateMutexW(None, False, name)
        if not h:
            return True                          # can't tell: don't block startup
        if ctypes.get_last_error() == 183:       # ERROR_ALREADY_EXISTS
            k.CloseHandle(ctypes.c_void_p(h))
            return False
        _handle = h
        return True
    except (AttributeError, OSError):            # not Windows
        return True
