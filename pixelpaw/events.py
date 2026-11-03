"""In-process event bus feeding the OBS overlay and the dashboard (Server-Sent Events)."""
from __future__ import annotations

import threading
import time
from collections import deque


class EventBus:
    def __init__(self, keep: int = 100):
        self._events: deque[dict] = deque(maxlen=keep)
        self._cond = threading.Condition()
        self._seq = 0

    def emit(self, kind: str, **data) -> dict:
        with self._cond:
            self._seq += 1
            event = {"id": self._seq, "kind": kind, "ts": time.time(), **data}
            self._events.append(event)
            self._cond.notify_all()
        return event

    def since(self, last_id: int) -> list[dict]:
        with self._cond:
            return [e for e in self._events if e["id"] > last_id]

    def wait(self, last_id: int, timeout: float) -> list[dict]:
        with self._cond:
            self._cond.wait_for(lambda: self._seq > last_id, timeout=timeout)
            return [e for e in self._events if e["id"] > last_id]

    @property
    def last_id(self) -> int:
        return self._seq


class LiveState:
    """What the dashboard shows about the current stream."""

    def __init__(self):
        self.active = False
        self.title = ""
        self.chatters = 0

    def as_dict(self) -> dict:
        return {"active": self.active, "title": self.title, "chatters": self.chatters}


bus = EventBus()
live_state = LiveState()
