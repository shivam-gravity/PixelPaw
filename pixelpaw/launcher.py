"""Double-click entry point. Starts PixelPaw in the background (once) and opens the dashboard.

- Already running?  -> just open the dashboard.
- Not running?      -> start the service (no console window) and open the dashboard
                       (it shows Setup on first launch).
- Fatal problem?    -> a small message box explains it (no terminal needed).
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import urllib.request
import webbrowser

log = logging.getLogger(__name__)


def running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == "pixelpaw"
    except Exception:
        return False


def message_box(title: str, text: str) -> None:
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(title, text)
        root.destroy()
    except Exception:
        if sys.stderr:
            print(f"{title}: {text}", file=sys.stderr)


def _startup_error(exc: BaseException) -> None:
    """Without a console, write the full error next to the logs before anything else."""
    import traceback
    from pathlib import Path

    try:
        from .config import ROOT

        path = Path(ROOT) / "data" / "logs" / "startup-error.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), encoding="utf-8")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    no_browser = "--no-browser" in argv
    try:
        from .app import App
        from .health import setup_logging

        app = App()
    except Exception as exc:
        _startup_error(exc)
        message_box("PixelPaw couldn't start", f"{type(exc).__name__}: {exc}")
        return 1

    url = f"http://127.0.0.1:{app.settings.web_port}/"
    if running(app.settings.web_port):
        if not no_browser:
            webbrowser.open(url)
        return 0

    log_file = setup_logging(app.settings.data_dir / "logs", verbose="-v" in argv)
    log.info("Starting PixelPaw", extra={"event": "start", "log_file": str(log_file)})
    from .service import Service

    service = Service(app)
    if not no_browser:
        threading.Timer(1.2, webbrowser.open, [url]).start()
    try:
        ok = service.run()
    except Exception as exc:  # last-resort: never die silently
        log.exception("PixelPaw crashed")
        message_box("PixelPaw stopped unexpectedly", f"{exc}\n\nDetails are in {log_file}")
        return 1
    if not ok:
        _startup_error(OSError(f"port {app.settings.web_port} is used by another program"))
        message_box("PixelPaw couldn't start",
                    f"Port {app.settings.web_port} is used by another program.\n"
                    f"Set PIXELPAW_WEB_PORT to another number (e.g. 8770) in the .env file and try again.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
