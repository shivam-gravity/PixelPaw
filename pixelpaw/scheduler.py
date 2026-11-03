"""Routines (modelled on DailyBot's scheduled check-ins): timezone-aware recurring jobs.

Standard library only:
- comments sweep every N minutes
- daily report at a fixed local time
- live-stream detector that starts the chat bot when you go live
- the overlay/dashboard web server, in a background thread
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import comments, report
from .livechat import LiveWatcher

log = logging.getLogger(__name__)


class Job:
    def __init__(self, name, fn, every_minutes=None, daily_at=None, run_now=False, tz=None):
        self.name, self.fn, self.every, self.daily_at, self.tz = name, fn, every_minutes, daily_at, tz
        self.next_run = datetime.now(tz) if run_now else self._following(datetime.now(tz))

    def _following(self, now: datetime) -> datetime:
        if self.every:
            return now + timedelta(minutes=self.every)
        hour, minute = self.daily_at
        nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return nxt if nxt > now else nxt + timedelta(days=1)

    def run(self) -> None:
        try:
            self.fn()
        except Exception:
            log.exception("Routine '%s' failed", self.name)
        self.next_run = self._following(datetime.now(self.tz))


def run_forever(app, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    tz = ZoneInfo(app.settings.timezone)
    p = app.persona
    hour, minute = (int(x) for x in str(p["report"]["time"]).split(":"))

    from .web import start_web

    web = start_web(app)
    jobs = [
        Job("comments", lambda: comments.sweep(app), every_minutes=p["comments"]["check_every_minutes"], run_now=True, tz=tz),
        Job("daily_report", lambda: report.daily_report(app), daily_at=(hour, minute), tz=tz),
    ]
    watcher = None
    if p["live_chat"]["auto_detect"]:
        watcher = LiveWatcher(app)
        jobs.append(Job("live", watcher.check, every_minutes=p["live_chat"]["detect_every_minutes"], run_now=True, tz=tz))

    log.info(
        "🐾 PixelPaw running (%s). Comments every %s min [%s mode], report daily at %02d:%02d%s. Ctrl+C to stop.",
        tz, p["comments"]["check_every_minutes"], p["comments"]["mode"], hour, minute,
        ", live chat auto-detect on" if watcher else "",
    )
    try:
        while not stop.is_set():
            now = datetime.now(tz)
            for job in sorted(jobs, key=lambda j: j.next_run):
                if job.next_run <= now and not stop.is_set():
                    job.run()  # one at a time
            wake = min(j.next_run for j in jobs)
            stop.wait(max(1.0, min(30.0, (wake - datetime.now(tz)).total_seconds())))
    except KeyboardInterrupt:
        pass
    finally:
        if watcher:
            watcher.shutdown()
        if web:
            web.shutdown()
        log.info("Stopped.")
