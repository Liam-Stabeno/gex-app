"""
sse.py — Server-Sent Events client management.

Maintains a list of connected browser queues and broadcasts JSON messages
to all of them. Thread-safe.

Public API:
    push(data: dict)   — broadcast a JSON message to all connected clients
    clients            — the raw list (used by the /api/stream route)
    lock               — the lock guarding clients (used by /api/stream route)
"""

import json
import threading
from queue import Queue

clients: list = []          # one Queue per connected browser tab
lock = threading.Lock()


def push(data: dict):
    """Broadcast a JSON SSE message to all connected clients."""
    msg = f"data: {json.dumps(data)}\n\n"
    with lock:
        for q in list(clients):
            try:
                q.put_nowait(msg)
            except Exception:
                pass  # full queue — client too slow, skip
