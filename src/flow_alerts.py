"""
flow_alerts.py — Daily flow alert persistence.

Stores streaming flow alerts to data/flow_alerts_YYYY-MM-DD.json so the
feed survives app restarts. Each day gets its own file.

Public API:
    load_today()          — call at startup to restore today's alerts
    append(alert)         — add a new alert and save
    get_all() -> list     — return all alerts for today (for /api/flow_alerts)
"""

import os
import json
import threading
from datetime import datetime

# Resolve data/ relative to project root (one level up from src/)
_SRC_DIR     = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SRC_DIR)
_DATA_DIR    = os.path.join(_PROJECT_DIR, 'data')

_alerts      = []
_alerts_lock = threading.Lock()


def _path(date_str: str) -> str:
    return os.path.join(_DATA_DIR, f'flow_alerts_{date_str}.json')


def _save():
    """Write all alerts to today's file. Must be called inside _alerts_lock."""
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_path(today), 'w') as f:
            json.dump(_alerts, f)
    except Exception as e:
        print(f'[ERROR] flow_alerts save: {e}')


def load_today():
    """Load today's persisted alerts on startup."""
    today = datetime.now().strftime('%Y-%m-%d')
    path  = _path(today)
    if not os.path.exists(path):
        print('[flow_alerts] No saved alerts for today — starting fresh')
        return
    try:
        with open(path) as f:
            saved = json.load(f)
        with _alerts_lock:
            _alerts.extend(saved)
        print(f'[flow_alerts] Restored {len(saved)} alerts from today')
    except Exception as e:
        print(f'[ERROR] flow_alerts load: {e}')


def append(alert: dict):
    """Add a new alert and persist immediately."""
    with _alerts_lock:
        _alerts.append(alert)
        _save()


def get_all() -> list:
    """Return a snapshot of all today's alerts."""
    with _alerts_lock:
        return list(_alerts)
