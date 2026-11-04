"""Budgets: YouTube API quota (live chat first) and daily AI calls.

YouTube's free quota is 10,000 units/day, reset at midnight Pacific time. PixelPaw tracks every call's
cost locally, keeps a reserve that only live chat may use, and stops normal work before the limit.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .net import ApiError

PACIFIC = ZoneInfo("America/Los_Angeles")


def youtube_cost(method: str, path: str) -> int:
    """Approximate YouTube Data API v3 cost of one request."""
    if path.endswith("/liveChat/messages"):
        return 5 if method == "GET" else 50
    if method in ("POST", "PUT", "DELETE"):
        return 50
    return 1


def is_live_path(path: str) -> bool:
    return "/liveChat/" in path or path.endswith("/liveBroadcasts")


class QuotaBudgetExceeded(ApiError):
    def __init__(self, message: str):
        super().__init__(429, "localQuotaBudget", message)


class QuotaTracker:
    def __init__(self, store, daily_limit: int = 10_000, live_reserve: int = 3_000):
        self.store = store
        self.daily_limit = int(daily_limit)
        self.live_reserve = int(live_reserve)
        self._lock = threading.Lock()

    @staticmethod
    def day() -> str:
        return datetime.now(PACIFIC).date().isoformat()

    def used(self) -> dict[str, int]:
        u = self.store.quota_used(self.day())
        return {"live": u.get("live", 0), "normal": u.get("normal", 0), "total": sum(u.values())}

    def exhausted(self) -> bool:
        return self.store.kv_get("quota_exhausted_day") == self.day()

    def check(self, cost: int, live: bool) -> None:
        """Raise QuotaBudgetExceeded instead of spending units we shouldn't."""
        with self._lock:
            if self.exhausted():
                raise QuotaBudgetExceeded("YouTube reported the daily quota is used up; it resets at midnight Pacific.")
            total = self.used()["total"]
            cap = self.daily_limit if live else self.daily_limit - self.live_reserve
            if total + cost > cap:
                who = "live chat" if live else f"normal work (the last {self.live_reserve} units are kept for live chat)"
                raise QuotaBudgetExceeded(f"Daily YouTube budget reached for {who}: {total}/{self.daily_limit} units used.")

    def charge(self, cost: int, live: bool) -> None:
        if cost:
            self.store.quota_add(self.day(), "live" if live else "normal", cost)

    def mark_exhausted(self) -> None:
        self.store.kv_set("quota_exhausted_day", self.day())

    def remaining(self) -> int:
        return max(0, self.daily_limit - self.used()["total"])

    def poll_multiplier(self) -> float:
        """Slow live-chat polling down as the budget runs out."""
        left = self.remaining()
        return 6.0 if left < 600 else 3.0 if left < 1500 else 1.0

    def status(self) -> dict:
        now = datetime.now(PACIFIC)
        reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        u = self.used()
        return {**u, "limit": self.daily_limit, "live_reserve": self.live_reserve, "remaining": self.remaining(),
                "exhausted": self.exhausted(), "resets_at": reset.isoformat(), "day": self.day()}


class AiBudget:
    """Counts AI calls per local day and enforces a daily cap."""

    def __init__(self, store, max_calls_per_day: int = 400):
        self.store = store
        self.max_calls = int(max_calls_per_day)

    @staticmethod
    def day() -> str:
        return datetime.now().date().isoformat()

    def check(self) -> None:
        from .llm import LLMError

        if self.store.ai_used(self.day())["calls"] >= self.max_calls:
            raise LLMError(f"Daily AI budget reached ({self.max_calls} calls). It resets at midnight; change it in Settings.")

    def record(self, ok: bool) -> None:
        self.store.ai_add(self.day(), "calls" if ok else "failures")

    def record_cached(self) -> None:
        self.store.ai_add(self.day(), "cached")

    def status(self) -> dict:
        return {**self.store.ai_used(self.day()), "limit": self.max_calls}
