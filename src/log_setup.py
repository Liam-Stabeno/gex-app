"""
log_setup.py — Centralised logging for the GEX dashboard.

Call setup_logging() once at startup (in dashboard.py __main__).
After that, all print() output AND logging.* calls are written to both
the console and  logs/app_YYYY-MM-DD.log.

Log files rotate daily; the last 14 days are kept automatically.
"""

import os
import sys
import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

LOGS_DIR = 'logs'


class _Tee:
    """Write to multiple streams simultaneously (used to mirror stdout/stderr)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()

    # Proxy everything else to the first (real) stream
    def __getattr__(self, name):
        return getattr(self.streams[0], name)


def setup_logging(log_dir: str = LOGS_DIR, keep_days: int = 14) -> logging.Logger:
    """
    Configure file + console logging.

    Returns the root logger so callers can do:
        log = setup_logging()
        log.info('started')
    """
    os.makedirs(log_dir, exist_ok=True)

    # Daily rotating log file: logs/app_2026-05-22.log
    today = datetime.now().strftime('%Y-%m-%d')
    log_path = os.path.join(log_dir, f'app_{today}.log')

    # Root logger — catches everything
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt='%(asctime)s  %(levelname)-8s  %(message)s',
        datefmt='%H:%M:%S'
    )

    # File handler — rotates at midnight, keeps last `keep_days` files
    fh = TimedRotatingFileHandler(
        log_path,
        when='midnight',
        interval=1,
        backupCount=keep_days,
        encoding='utf-8',
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    fh.suffix = '%Y-%m-%d'          # rename rotated files to app_YYYY-MM-DD.log.YYYY-MM-DD
    root.addHandler(fh)

    # Console handler — INFO and above (less noise on screen)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Mirror raw print() / sys.stdout / sys.stderr to the log file too.
    # This picks up Flask's built-in print statements, websocket-client messages, etc.
    file_stream = open(log_path, 'a', encoding='utf-8', buffering=1)
    sys.stdout = _Tee(sys.__stdout__, file_stream)
    sys.stderr = _Tee(sys.__stderr__, file_stream)

    logging.info(f'Logging started — writing to {log_path}')
    return root
