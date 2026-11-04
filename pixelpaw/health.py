"""Observability: structured JSON logs, per-component health, and a recent-errors buffer.

- data/logs/pixelpaw.jsonl : one JSON object per line, rotated (5 x 1 MB)
- health.snapshot()        : what the dashboard's Health tab shows
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import threading
import time
from collections import deque
from pathlib import Path

START = time.time()
_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        data = {
            "ts": round(r.created, 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r.created)),
            "level": r.levelname,
            "logger": r.name,
            "msg": r.getMessage(),
        }
        for key, value in r.__dict__.items():  # fields passed with extra={...}
            if key not in _STD_ATTRS and not key.startswith("_"):
                try:
                    json.dumps(value)
                    data[key] = value
                except TypeError:
                    data[key] = str(value)
        if r.exc_info:
            data["exc"] = self.formatException(r.exc_info)
        return json.dumps(data, ensure_ascii=False)


class Health:
    def __init__(self):
        self._lock = threading.Lock()
        self.components: dict[str, dict] = {}
        self.errors: deque[dict] = deque(maxlen=200)

    def _set(self, name: str, status: str, detail: str) -> None:
        now = time.time()
        with self._lock:
            c = self.components.setdefault(name, {"status": "unknown", "detail": "", "since": now,
                                                  "last_ok": None, "last_error": None, "failures": 0})
            if c["status"] != status:
                c["since"] = now
            c["status"], c["detail"] = status, detail[:300]
            if status == "ok":
                c["last_ok"], c["failures"] = now, 0
            elif status == "error":
                c["last_error"] = now
                c["failures"] += 1

    def ok(self, name: str, detail: str = "") -> None:
        self._set(name, "ok", detail)

    def warn(self, name: str, detail: str) -> None:
        self._set(name, "warn", detail)

    def fail(self, name: str, detail: str) -> None:
        self._set(name, "error", detail)

    def status(self, name: str) -> str:
        with self._lock:
            return self.components.get(name, {}).get("status", "unknown")

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            comps = {k: dict(v) for k, v in self.components.items()}
            errors = list(self.errors)
        day = [e for e in errors if now - e["ts"] < 86400]
        worst = "ok"
        for c in comps.values():
            if c["status"] == "error":
                worst = "error"
            elif c["status"] == "warn" and worst == "ok":
                worst = "warn"
        return {
            "status": worst,
            "uptime_s": int(now - START),
            "components": comps,
            "errors": list(reversed(errors[-50:])),
            "counts_24h": {"error": sum(e["level"] in ("ERROR", "CRITICAL") for e in day),
                           "warning": sum(e["level"] == "WARNING" for e in day)},
        }


health = Health()


class _Collector(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            health.errors.append({"ts": record.created, "level": record.levelname, "logger": record.name,
                                  "msg": record.getMessage()[:500]})
        except Exception:
            pass


def setup_logging(log_dir: Path, verbose: bool = False) -> Path:
    """Console (when there is one) + rotating JSON file + error collector. Safe to call twice."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_pixelpaw", False):
            root.removeHandler(h)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    if sys.stderr is not None:  # pythonw has no console
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        console._pixelpaw = True
        root.addHandler(console)

    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "pixelpaw.jsonl"
    file = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(JsonFormatter())
    file._pixelpaw = True
    root.addHandler(file)

    collector = _Collector(level=logging.WARNING)
    collector._pixelpaw = True
    root.addHandler(collector)
    return path
