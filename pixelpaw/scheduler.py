"""Compatibility wrapper: the scheduler now lives in service.py (`python -m pixelpaw run` still works)."""
from __future__ import annotations

from .service import Job, Service

__all__ = ["Job", "Service", "run_forever"]


def run_forever(app, stop=None) -> None:
    svc = Service(app)
    if stop is not None:
        svc.stop_event = stop
    svc.run()
