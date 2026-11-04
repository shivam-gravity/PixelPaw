"""The PixelPaw background service: web dashboard + overlay, scheduled routines, live-chat watcher.

Started by the double-click launcher (or OBS); users never need a terminal.
Routines are timezone-aware (like DailyBot check-ins) and recover on their own: a failed job retries
with exponential backoff (1, 2, 4 … 30 min), and jobs wait politely until setup is finished.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .health import health
from .youtube import NotAuthorised

log = logging.getLogger(__name__)


class Job:
    def __init__(self, name, fn, every_minutes=None, daily_at=None, run_now=False, tz=None, needs_setup=False):
        self.name, self.fn, self.every, self.daily_at, self.tz = name, fn, every_minutes, daily_at, tz
        self.needs_setup = needs_setup
        self.failures = 0
        self.last_run: datetime | None = None
        self.next_run = datetime.now(tz) if run_now else self._following(datetime.now(tz))

    def _following(self, now: datetime) -> datetime:
        if self.every:
            return now + timedelta(minutes=self.every)
        hour, minute = self.daily_at
        nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return nxt if nxt > now else nxt + timedelta(days=1)

    def run(self) -> None:
        now = datetime.now(self.tz)
        self.last_run = now
        try:
            self.fn()
            self.failures = 0
            health.ok(f"job:{self.name}", f"last run {now:%H:%M}")
            self.next_run = self._following(datetime.now(self.tz))
        except NotAuthorised as exc:
            self.failures += 1
            health.fail(f"job:{self.name}", str(exc))
            self.next_run = datetime.now(self.tz) + timedelta(minutes=30)
        except Exception as exc:
            self.failures += 1
            log.exception("Routine '%s' failed (attempt %d)", self.name, self.failures,
                          extra={"event": "job_failed", "job": self.name})
            health.fail(f"job:{self.name}", f"{type(exc).__name__}: {exc}"[:200])
            retry = datetime.now(self.tz) + timedelta(minutes=min(30, 2 ** (self.failures - 1)))
            normal = self._following(datetime.now(self.tz))
            self.next_run = retry if (self.failures <= 6 and retry < normal) else normal

    def as_dict(self) -> dict:
        return {"name": self.name, "next_run": self.next_run.isoformat(timespec="minutes"),
                "last_run": self.last_run.isoformat(timespec="minutes") if self.last_run else None,
                "failures": self.failures}


class Service:
    def __init__(self, app):
        self.app = app
        self.stop_event = threading.Event()
        self.web = None
        self.watcher = None
        self.jobs: list[Job] = []
        self._run_lock = threading.Lock()  # one routine at a time (dashboard actions included)

    # ---- jobs -------------------------------------------------------------
    def build_jobs(self) -> None:
        from . import comments, report
        from .livechat import LiveWatcher

        tz = ZoneInfo(self.app.settings.timezone)
        p = self.app.persona
        hour, minute = (int(x) for x in str(p["report"]["time"]).split(":"))
        if self.watcher:
            self.watcher.shutdown()
        self.jobs = [
            Job("comments", lambda: comments.sweep(self.app), every_minutes=p["comments"]["check_every_minutes"],
                run_now=True, tz=tz, needs_setup=True),
            Job("daily_report", lambda: report.daily_report(self.app), daily_at=(hour, minute), tz=tz, needs_setup=True),
        ]
        self.watcher = None
        if p["live_chat"]["auto_detect"]:
            self.watcher = LiveWatcher(self.app)
            self.jobs.append(Job("live", self.watcher.check, every_minutes=p["live_chat"]["detect_every_minutes"],
                                 run_now=True, tz=tz, needs_setup=True))

    def kick(self, name: str | None = None) -> None:
        """Run a job (or all) at the next tick, e.g. right after setup finishes."""
        for job in self.jobs:
            if name is None or job.name == name:
                job.next_run = datetime.now(job.tz)

    def run_exclusive(self, fn):
        with self._run_lock:
            return fn()

    # ---- web --------------------------------------------------------------
    def start_web(self):
        from .web import start_web

        self.web = start_web(self.app, self)
        return self.web

    def restart_web(self) -> None:
        old = self.web
        if old:
            threading.Thread(target=old.shutdown, daemon=True).start()
        self.app.reload()
        threading.Timer(0.5, self.start_web).start()

    # ---- main loop --------------------------------------------------------
    def run(self) -> bool:
        if not self.start_web():
            return False
        self.build_jobs()
        tz = ZoneInfo(self.app.settings.timezone)
        log.info("🐾 PixelPaw is running. Dashboard: http://127.0.0.1:%s/", self.app.settings.web_port)
        try:
            while not self.stop_event.is_set():
                now = datetime.now(tz)
                ready = self.app.youtube_ready()
                if not ready:
                    health.warn("setup", "finish setup in the dashboard to start the bot")
                else:
                    health.ok("setup", "complete")
                for job in sorted(self.jobs, key=lambda j: j.next_run):
                    if job.next_run > now or self.stop_event.is_set():
                        continue
                    if job.needs_setup and not ready:
                        job.next_run = now + timedelta(minutes=1)
                        continue
                    self.run_exclusive(job.run)
                wake = min((j.next_run for j in self.jobs), default=now + timedelta(seconds=30))
                self.stop_event.wait(max(1.0, min(15.0, (wake - datetime.now(tz)).total_seconds())))
        except KeyboardInterrupt:
            pass
        finally:
            if self.watcher:
                self.watcher.shutdown()
            if self.web:
                self.web.shutdown()
            log.info("PixelPaw stopped.")
        return True

    def shutdown(self) -> None:
        self.stop_event.set()

    def status(self) -> dict:
        return {"jobs": [j.as_dict() for j in self.jobs]}
