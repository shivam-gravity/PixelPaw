"""Built-in web server (standard library only) for the OBS overlay and the dashboard.

  /overlay   OBS Browser Source: transparent, animated alerts (Super Chats, members, !ask answers)
  /dock      Dashboard for an OBS Custom Browser Dock *and* your phone: live status, stats,
             approve/edit reply drafts, activity feed. Needs the token printed at startup.

Private data (/api/*, full event feed) always requires the token. The overlay's public feed only
carries what is already shown on stream.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .events import bus, live_state
from .i18n import available, strings

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
PUBLIC_KINDS = {"super_chat", "member", "ask", "live", "test"}


def web_token(app) -> str:
    path = app.settings.db_path.parent / "web_token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(18)
    path.write_text(token, encoding="utf-8")
    return token


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def make_handler(app, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PixelPaw"

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        # ---- helpers ------------------------------------------------
        def _query(self) -> dict:
            return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))

        def _authed(self) -> bool:
            given = self.headers.get("X-PixelPaw-Token") or self._query().get("token", "")
            return bool(given) and hmac.compare_digest(given, token)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _page(self, name: str) -> None:
            self._send(200, (WEB_DIR / name).read_bytes(), "text/html; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n or n > 20_000:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def _stream(self, kinds: set[str] | None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                last = int(self.headers.get("Last-Event-ID") or self._query().get("last", bus.last_id))
            except ValueError:
                last = bus.last_id
            try:
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()
                while True:
                    events = bus.wait(last, timeout=15)
                    if not events:
                        self.wfile.write(b": ping\n\n")
                    for ev in events:
                        last = ev["id"]
                        if kinds is None or ev["kind"] in kinds:
                            data = json.dumps(ev, ensure_ascii=False)
                            self.wfile.write(f"id: {ev['id']}\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

        # ---- routes -------------------------------------------------
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            q = self._query()
            if path == "/":
                self.send_response(302)
                self.send_header("Location", "/dock")
                self.end_headers()
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            elif path == "/overlay":
                self._page("overlay.html")
            elif path == "/dock":
                self._page("dock.html")
            elif path == "/events":
                self._stream(PUBLIC_KINDS)
            elif path == "/api/i18n":
                self._json(strings(q.get("lang") or app.persona["ui"]["language"]))
            elif path == "/api/config":
                ui = app.persona["ui"]
                self._json({"lang": ui["language"], "overlay": ui["overlay"], "languages": available()})
            elif not self._authed():
                self._json({"error": "missing or wrong token"}, 401)
            elif path == "/api/events":
                self._stream(None)
            elif path == "/api/state":
                snap = app.store.latest_snapshot()
                self._json({
                    "channel": app.persona["channel"]["name"],
                    "live": live_state.as_dict(),
                    "snapshot": snap,
                    "pending": len(app.store.pending()),
                    "mode": app.persona["comments"]["mode"],
                    "recent": bus.since(max(0, bus.last_id - 30)),
                })
            elif path == "/api/drafts":
                rows = app.store.pending()
                self._json([{k: r.get(k) for k in ("id", "author", "text", "video_title", "category", "sentiment", "draft", "note")}
                            for r in rows[:50]])
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            if not self._authed():
                return self._json({"error": "missing or wrong token"}, 401)
            if path == "/api/test":
                s = strings(app.persona["ui"]["language"])
                bus.emit("test", name="PixelPaw", amount="₹100", message=s.get("test_message", "Test"))
                return self._json({"ok": True})
            parts = path.split("/")  # /api/drafts/<id>/<action>
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "drafts":
                comment_id, action = urllib.parse.unquote(parts[3]), parts[4]
                from . import comments

                if action == "discard":
                    comments.discard_draft(app, comment_id)
                    return self._json({"status": "discarded"})
                if action == "approve":
                    body = self._body()
                    try:
                        status, msg = comments.approve_draft(app, comment_id, body.get("text"), bool(body.get("force")))
                    except Exception as exc:  # NotAuthorised / LLMError / network
                        status, msg = "error", str(exc)
                    return self._json({"status": status, "message": msg})
            self._json({"error": "not found"}, 404)

    return Handler


def start_web(app) -> ThreadingHTTPServer | None:
    host, port = app.settings.web_host, app.settings.web_port
    token = web_token(app)
    try:
        server = ThreadingHTTPServer((host, port), make_handler(app, token))
    except OSError as exc:
        log.error("Web server could not start on %s:%s (%s). Set PIXELPAW_WEB_PORT in .env.", host, port, exc)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    local = f"http://127.0.0.1:{port}"
    log.info("🎥 OBS overlay (Browser Source):  %s/overlay", local)
    log.info("🧭 Dashboard / OBS dock:           %s/dock?token=%s", local, token)
    if host in ("0.0.0.0", "::"):
        log.info("📱 On your phone (same Wi-Fi):     http://%s:%s/dock?token=%s", lan_ip(), port, token)
    return server
