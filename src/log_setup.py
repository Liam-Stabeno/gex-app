"""
log_setup.py — Centralised logging for the GEX dashboard.

Call setup_logging() once at startup (in dashboard.py __main__).
After that, all print() output AND logging.* calls are written to both
the console and  logs/app_YYYY-MM-DD.log.

Log files roll at midnight by opening a NEW dated file — no os.rename()
is used, which avoids the Windows "file in use" PermissionError that
TimedRotatingFileHandler hits when any other thread still has the file open.
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from glob import glob


LOGS_DIR = 'logs'


# ── Custom daily-rollover handler ────────────────────────────────────────────

class _DailyFileHandler(logging.FileHandler):
    """
    Opens a new log file named app_YYYY-MM-DD.log when the calendar day
    changes.  Uses open-new-file semantics instead of os.rename(), so it
    works correctly on Windows even when other threads hold the log open.
    Old files beyond keep_days are pruned on each rollover.
    """

    def __init__(self, log_dir: str, keep_days: int = 14):
        self.log_dir   = log_dir
        self.keep_days = keep_days
        self._today    = datetime.now().date()
        super().__init__(
            self._dated_path(self._today),
            mode='a',
            encoding='utf-8',
            delay=False,
        )

    def _dated_path(self, d) -> str:
        return os.path.join(self.log_dir, f'app_{d}.log')

    def _rollover_if_needed(self):
        today = datetime.now().date()
        if today == self._today:
            return
        # Day changed — close old stream, open new one (no rename needed)
        try:
            if self.stream:
                self.stream.flush()
                self.stream.close()
        except Exception:
            pass
        self._today        = today
        self.baseFilename  = os.path.abspath(self._dated_path(today))
        self.stream        = self._open()
        self._prune_old(today)

    def emit(self, record):
        try:
            self._rollover_if_needed()
        except Exception:
            pass
        super().emit(record)

    def _prune_old(self, today):
        cutoff = today - timedelta(days=self.keep_days)
        for path in glob(os.path.join(self.log_dir, 'app_*.log')):
            try:
                date_str = os.path.basename(path)[4:14]   # "2026-05-22"
                if datetime.strptime(date_str, '%Y-%m-%d').date() < cutoff:
                    os.remove(path)
            except Exception:
                pass


# ── stdout/stderr mirror ─────────────────────────────────────────────────────

class _HandlerStream:
    """
    Proxy that always writes to the *current* stream of a FileHandler.
    Because _DailyFileHandler swaps self.stream on rollover, this proxy
    stays valid across midnight without needing its own file handle.
    """

    def __init__(self, handler: logging.FileHandler):
        self._handler = handler

    def write(self, data: str):
        s = self._handler.stream
        if s and data:
            s.write(data)
            s.flush()

    def flush(self):
        s = self._handler.stream
        if s:
            s.flush()

    def __getattr__(self, name):
        return getattr(sys.__stdout__, name)


class _Tee:
    """Write to multiple streams simultaneously."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self.streams[0], name)


# ── Public entry point ───────────────────────────────────────────────────────

def setup_logging(log_dir: str = LOGS_DIR, keep_days: int = 14) -> logging.Logger:
    """
    Configure file + console logging.
    Returns the root logger so callers can do:
        log = setup_logging()
        log.info('started')
    """
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt='%(asctime)s  %(levelname)-8s  %(message)s',
        datefmt='%H:%M:%S',
    )

    # File handler — rolls at midnight by opening a new file (no rename)
    fh = _DailyFileHandler(log_dir, keep_days=keep_days)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler — INFO and above
    ch = logging.StreamHandler(sys.__stdout__)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Mirror print() / sys.stdout / sys.stderr into the log file.
    # _HandlerStream always points at fh.stream, even after a rollover.
    log_mirror = _HandlerStream(fh)
    sys.stdout = _Tee(sys.__stdout__, log_mirror)
    sys.stderr = _Tee(sys.__stderr__, log_mirror)

    logging.info(f'Logging started — writing to {fh.baseFilename}')
    return root
