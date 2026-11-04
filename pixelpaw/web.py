"""Built-in web server (standard library only): the PixelPaw app (setup + dashboard) and the OBS overlay.

  /          the app: Setup, Home, Drafts, Create, Health, Settings (works as an OBS dock and on phones)
  /overlay   OBS Browser Source (transparent, animated alerts)

Security
  - Sessions: an HttpOnly, SameSite=Strict cookie (an HMAC of the secret token, never the token itself).
    On this computer the app signs you in automatically; phones need the one-time link from Settings.
  - Writes need the session cookie *and* a same-origin Origin header (or the token header): no CSRF.
  - Host header must be local/private (blocks DNS-rebinding attacks from websites).
  - Only loopback / private-network clients are served; public IPs are refused.
  - Secrets (AI keys, Google client, sign-in, network/security changes) can only be changed from this computer.
  - Repeated bad tokens lock that IP out for 10 minutes. Strict CSP and no-framing headers on every page.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .events import bus, live_state
from .health import health
from .i18n import available, strings

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
PUBLIC_KINDS = {"super_chat", "member", "ask", "live", "test"}
COOKIE = "pp_session"
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
SETTINGS_SECTIONS = {"channel", "voice", "links", "faq", "comments", "live_chat", "report", "ui", "quota", "ai"}
POSITIONS = {"bottom-left", "bottom-right", "top-left", "top-right", "center"}

_overlay_lock = threading.Lock()
_overlay_clients = 0
_oauth_pending: dict[str, dict] = {}


# ---- token / network helpers ------------------------------------------------------
def web_token(app) -> str:
    path = app.settings.db_path.parent / "web_token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return rotate_token(app)


def rotate_token(app) -> str:
    path = app.settings.db_path.parent / "web_token"
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path.write_text(token, encoding="utf-8")
    return token


def session_value(token: str) -> str:
    return hmac.new(token.encode(), b"pixelpaw-session-v1", hashlib.sha256).hexdigest()


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def is_allowed_client(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def is_loopback(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
        return ip.is_loopback or bool(getattr(ip, "ipv4_mapped", None) and ip.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def is_allowed_host(host_header: str) -> bool:
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]")[0]
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


class Lockout:
    def __init__(self, limit: int = 20, window: float = 600):
        self.limit, self.window = limit, window
        self.fails: dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            return sum(1 for t in self.fails[ip] if now - t < self.window) >= self.limit

    def fail(self, ip: str) -> None:
        with self._lock:
            self.fails[ip].append(time.time())


# ---- settings shape for the dashboard ----------------------------------------------
def settings_view(app) -> dict:
    p, s = app.persona, app.settings
    from . import autostart

    def pick(d, keys):
        return {k: d.get(k) for k in keys}

    port = s.web_port
    return {
        "channel": pick(p["channel"], ["name", "handle", "niche", "audience", "language"]),
        "voice": pick(p["voice"], ["tone", "signature", "style_rules", "never", "max_reply_chars"]),
        "links": {k: v for k, v in (p.get("links") or {}).items() if v},
        "faq": [f for f in p.get("faq") or [] if f.get("q")],
        "comments": pick(p["comments"], ["mode", "reply_to", "max_replies_per_day", "check_every_minutes"]),
        "live_chat": {**pick(p["live_chat"], ["greeting", "banned_words", "ai_label", "ask_per_user_per_hour", "auto_detect"]),
                      "commands": {k: v for k, v in (p["live_chat"].get("commands") or {}).items() if v}},
        "report": pick(p["report"], ["time", "deliver_to"]),
        "ui": p["ui"],
        "quota": p["quota"],
        "ai": p["ai"],
        "system": {
            "provider": s.llm_provider,
            "model": s.gemini_model if s.llm_provider == "gemini" else s.openai_model,
            "timezone": s.timezone,
            "lan": s.web_host in ("0.0.0.0", "::"),
            "port": port,
            "autostart": _safe(autostart.is_enabled, False),
            "discord": bool(s.discord_webhook_url),
            "telegram": bool(s.telegram_bot_token and s.telegram_chat_id),
            "languages": available(),
            "overlay_url": f"http://127.0.0.1:{port}/overlay",
            "dock_url": f"http://127.0.0.1:{port}/",
            "log_file": str(s.data_dir / "logs" / "pixelpaw.jsonl"),
        },
    }


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def clean_settings_patch(app, body: dict) -> dict:
    """Whitelist + light validation of what the dashboard may change."""
    patch: dict = {}
    for section, values in (body or {}).items():
        if section not in SETTINGS_SECTIONS:
            continue
        if section == "faq":
            if isinstance(values, list):
                patch["faq"] = [{"q": str(f.get("q", ""))[:300], "a": str(f.get("a", ""))[:500]}
                                for f in values if isinstance(f, dict) and str(f.get("q", "")).strip()][:100]
            continue
        if not isinstance(values, dict):
            continue
        patch[section] = dict(values)
    c = patch.get("comments", {})
    if "mode" in c and c["mode"] not in ("review", "auto"):
        c.pop("mode")
    for key, lo, hi in (("max_replies_per_day", 0, 500), ("check_every_minutes", 5, 1440)):
        if key in c:
            c[key] = max(lo, min(hi, int(c[key])))
    ov = patch.get("ui", {}).get("overlay", {})
    if "position" in ov and ov["position"] not in POSITIONS:
        ov.pop("position")
    if "seconds" in ov:
        ov["seconds"] = max(2, min(60, int(ov["seconds"])))
    if "quota" in patch:
        q = patch["quota"]
        q["daily_limit"] = max(100, int(q.get("daily_limit", app.persona["quota"]["daily_limit"])))
        q["live_reserve"] = max(0, min(q["daily_limit"], int(q.get("live_reserve", app.persona["quota"]["live_reserve"]))))
    if "ai" in patch and "max_calls_per_day" in patch["ai"]:
        patch["ai"]["max_calls_per_day"] = max(0, int(patch["ai"]["max_calls_per_day"]))
    if "report" in patch and "time" in patch["report"]:
        hh, mm = str(patch["report"]["time"]).split(":")[:2]
        patch["report"]["time"] = f"{max(0, min(23, int(hh))):02d}:{max(0, min(59, int(mm))):02d}"
    # Dicts edited as a whole: removed keys become "" so they also hide entries from persona.yaml.
    for section, key in (("live_chat", "commands"), ("links", None)):
        if section in patch:
            new = patch[section].get(key) if key else patch[section]
            if isinstance(new, dict):
                old = (app.persona[section].get(key) if key else app.persona.get(section)) or {}
                full = {k: "" for k in old} | {str(k)[:40]: str(v)[:300] for k, v in new.items()}
                if key:
                    patch[section][key] = full
                else:
                    patch[section] = full
    return patch


# ---- request handler ------------------------------------------------------------------
def make_handler(app, service=None):
    lockout = Lockout()
    state = {"token": web_token(app)}

    class Handler(BaseHTTPRequestHandler):
        server_version = "PixelPaw"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        # ---- low level ------------------------------------------------
        @property
        def client(self) -> str:
            return self.client_address[0]

        def _q(self) -> dict:
            return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))

        def _path(self) -> str:
            return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

        def _cookie(self) -> str:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return ""

        def _headers(self, code: int, ctype: str, length: int | None, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self._headers(code, ctype, len(body), extra)
            self.wfile.write(body)

        def _json(self, data, code: int = 200, extra: dict | None = None) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        def _redirect(self, location: str, extra: dict | None = None) -> None:
            self._headers(303, "text/plain", 0, {"Location": location, **(extra or {})})

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n or n > 400_000:
                return {}
            try:
                data = json.loads(self.rfile.read(n).decode("utf-8"))
                return data if isinstance(data, dict) else {}
            except (ValueError, UnicodeDecodeError):
                return {}

        def _session_cookie(self) -> dict:
            return {"Set-Cookie": f"{COOKIE}={session_value(state['token'])}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"}

        # ---- auth -----------------------------------------------------
        def _token_ok(self, given: str) -> bool:
            return bool(given) and hmac.compare_digest(given, state["token"])

        def _authed(self, write: bool = False) -> bool:
            if self._token_ok(self.headers.get("X-PixelPaw-Token", "")):
                return True
            if self._cookie() and hmac.compare_digest(self._cookie(), session_value(state["token"])):
                if not write:
                    return True
                origin = self.headers.get("Origin", "")
                return bool(origin) and urllib.parse.urlparse(origin).netloc == self.headers.get("Host", "")
            return False

        def _guard(self) -> bool:
            """Network-level checks shared by every request. Returns False if a response was sent."""
            if not is_allowed_client(self.client):
                self._json({"error": "PixelPaw only serves this computer and your local network"}, 403)
                return False
            if not is_allowed_host(self.headers.get("Host", "")):
                self._json({"error": "unexpected Host header"}, 403)
                return False
            if lockout.blocked(self.client):
                self._json({"error": "too many wrong tokens; try again in 10 minutes"}, 429)
                return False
            return True

        def _deny(self) -> None:
            if self.headers.get("X-PixelPaw-Token") or self._cookie():
                lockout.fail(self.client)
            self._json({"error": "not signed in: open PixelPaw on this computer, or use the phone link from Settings"}, 401)

        # ---- pages ----------------------------------------------------
        def _app_page(self) -> None:
            extra = {}
            signed_in = self._cookie() and hmac.compare_digest(self._cookie(), session_value(state["token"]))
            cross_site = self.headers.get("Sec-Fetch-Site", "") == "cross-site"
            if not signed_in and is_loopback(self.client) and not cross_site:
                extra = self._session_cookie()  # same computer: sign in automatically
            self._send(200, (WEB_DIR / "app.html").read_bytes(), "text/html; charset=utf-8", extra)

        def _stream(self, kinds: set[str] | None) -> None:
            global _overlay_clients
            self._headers(200, "text/event-stream; charset=utf-8", None, {"X-Accel-Buffering": "no", "Connection": "keep-alive"})
            public = kinds is not None
            if public:
                with _overlay_lock:
                    _overlay_clients += 1
                    health.ok("overlay", f"{_overlay_clients} overlay(s) connected")
            try:
                last = int(self.headers.get("Last-Event-ID") or bus.last_id)
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
                            self.wfile.write(f"id: {ev['id']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                if public:
                    with _overlay_lock:
                        _overlay_clients -= 1
                        if _overlay_clients <= 0:
                            health.warn("overlay", "no overlay connected (add it in OBS)")
                self.close_connection = True

        # ---- GET ------------------------------------------------------
        def do_GET(self):
            if not self._guard():
                return
            path, q = self._path(), self._q()
            if path in ("/", "/app", "/setup"):
                return self._app_page()
            if path in ("/dock", "/login"):
                if self._token_ok(q.get("token", "")):
                    return self._redirect("/", self._session_cookie())
                if q.get("token"):
                    lockout.fail(self.client)
                return self._app_page()
            if path == "/overlay":
                return self._send(200, (WEB_DIR / "overlay.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/healthz":
                return self._json({"app": "pixelpaw", "ok": True})
            if path == "/events":
                return self._stream(PUBLIC_KINDS)
            if path == "/api/i18n":
                return self._json(strings(q.get("lang") or app.persona["ui"]["language"]))
            if path == "/api/config":
                ui = app.persona["ui"]
                return self._json({"lang": ui["language"], "overlay": ui["overlay"], "languages": available()})
            if path == "/oauth/callback":
                return self._oauth_callback(q)
            if not self._authed():
                return self._deny()
            if path == "/oauth/start":
                return self._oauth_start()
            if path == "/api/events":
                return self._stream(None)
            if path == "/api/state":
                return self._json(self._state())
            if path == "/api/drafts":
                rows = app.store.pending()
                return self._json([{k: r.get(k) for k in ("id", "author", "text", "video_title", "category", "sentiment", "draft", "note")}
                                   for r in rows[:50]])
            if path == "/api/health":
                return self._json(self._health())
            if path == "/api/settings":
                return self._json(settings_view(app))
            if path == "/api/report/latest":
                files = sorted(app.settings.reports_dir.glob("*.md")) if app.settings.reports_dir.exists() else []
                return self._json({"name": files[-1].name, "text": files[-1].read_text(encoding="utf-8")} if files else {})
            if path == "/api/security/phone-link":
                if not is_loopback(self.client):
                    return self._json({"error": "only from this computer"}, 403)
                lan = app.settings.web_host in ("0.0.0.0", "::")
                return self._json({"enabled": lan, "url": f"http://{lan_ip()}:{app.settings.web_port}/login?token={state['token']}" if lan else ""})
            return self._json({"error": "not found"}, 404)

        def _state(self) -> dict:
            snap = app.store.latest_snapshot()
            return {
                "channel": app.persona["channel"]["name"],
                "setup": app.setup_status(),
                "live": live_state.as_dict(),
                "snapshot": snap,
                "pending": len(app.store.pending()),
                "mode": app.persona["comments"]["mode"],
                "recent": bus.since(max(0, bus.last_id - 30)),
                "quota": app.quota.status(),
                "ai": app.ai_budget.status(),
                "health": health.snapshot()["status"],
            }

        def _health(self) -> dict:
            from . import __version__

            snap = health.snapshot()
            return {**snap, "version": __version__, "quota": app.quota.status(), "ai": app.ai_budget.status(),
                    "jobs": service.status()["jobs"] if service else [], "overlay_clients": _overlay_clients,
                    "log_file": str(app.settings.data_dir / "logs" / "pixelpaw.jsonl")}

        # ---- OAuth (Sign in with Google from the setup page) ------------
        def _oauth_start(self):
            if not is_loopback(self.client):
                return self._json({"error": "sign in from the computer running PixelPaw"}, 403)
            from .youtube import NotAuthorised, oauth_begin

            try:
                url, pending = oauth_begin(app.settings, f"http://127.0.0.1:{app.settings.web_port}/oauth/callback")
            except NotAuthorised as exc:
                return self._redirect("/#setup?error=" + urllib.parse.quote(str(exc)))
            _oauth_pending.clear()
            _oauth_pending[pending["state"]] = pending
            return self._redirect(url)

        def _oauth_callback(self, q: dict):
            pending = _oauth_pending.pop(q.get("state", ""), None)
            if not pending or time.time() - pending["created"] > 600:
                return self._redirect("/#setup?error=" + urllib.parse.quote("Sign-in expired or was opened twice. Try again."))
            if "code" not in q:
                return self._redirect("/#setup?error=" + urllib.parse.quote(q.get("error", "cancelled")))
            from .youtube import oauth_finish

            try:
                oauth_finish(app.settings, q["code"], pending)
                app.reload()
                name = app.yt.channel(refresh=True)["snippet"]["title"]
                if app.persona["channel"]["name"] in ("", "My Channel", "YOUR CHANNEL NAME"):
                    from .config import save_persona_overrides

                    save_persona_overrides(app.overrides_file, {"channel": {"name": name}})
                    app.reload_persona()
                if service:
                    service.kick()
            except Exception as exc:
                log.exception("Google sign-in failed")
                return self._redirect("/#setup?error=" + urllib.parse.quote(f"Sign-in failed: {exc}"[:300]))
            return self._redirect("/#setup?youtube=ok")

        # ---- POST -----------------------------------------------------
        def do_POST(self):
            if not self._guard():
                return
            if not self._authed(write=True):
                return self._deny()
            path, body = self._path(), self._body()
            try:
                return self._post(path, body)
            except Exception as exc:
                log.exception("Dashboard action %s failed", path)
                return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}, 500)

        def _run(self, fn):
            return service.run_exclusive(fn) if service else fn()

        def _post(self, path: str, body: dict):
            from . import comments, content

            if path == "/api/test":
                s = strings(app.persona["ui"]["language"])
                bus.emit("test", name="PixelPaw", amount="₹100", message=s.get("test_message", "Test"))
                return self._json({"ok": True})
            parts = path.split("/")
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "drafts":
                comment_id, action = urllib.parse.unquote(parts[3]), parts[4]
                if action == "discard":
                    comments.discard_draft(app, comment_id)
                    return self._json({"status": "discarded"})
                if action == "approve":
                    status, msg = comments.approve_draft(app, comment_id, body.get("text"), bool(body.get("force")))
                    return self._json({"status": status, "message": msg})
            if path == "/api/settings":
                from .config import save_persona_overrides, update_env

                patch = clean_settings_patch(app, body)
                if patch:
                    save_persona_overrides(app.overrides_file, patch)
                tz = (body.get("system") or {}).get("timezone")
                if tz:
                    from zoneinfo import ZoneInfo

                    ZoneInfo(tz)  # validates
                    update_env(app.settings.env_file, {"PIXELPAW_TIMEZONE": tz})
                    app.reload()
                else:
                    app.reload_persona()
                if service:
                    service.build_jobs()
                return self._json({"ok": True, "settings": settings_view(app)})
            if path == "/api/action/sweep":
                return self._json({"ok": True, "result": self._run(lambda: comments.sweep(app))})
            if path == "/api/action/report":
                from .report import daily_report

                return self._json({"ok": True, "text": self._run(lambda: daily_report(app, send=bool(body.get("send", True))))})
            if path == "/api/action/ideas":
                return self._json({"ok": True, "text": content.ideas(app, int(body.get("count") or 8), str(body.get("theme") or ""))})
            if path == "/api/action/package":
                return self._json({"ok": True, "text": content.package(app, str(body.get("topic") or "My next video"),
                                                                      transcript_text=str(body.get("transcript") or ""))})
            if path == "/api/action/learn":
                return self._json({"ok": True, "suggestion": content.suggest_persona(app)})
            if path == "/api/action/translate":
                from .i18n import translate_ui

                lang = str(body.get("lang") or "").strip()[:12]
                if not lang.replace("-", "").isalpha():
                    return self._json({"ok": False, "error": "use a language code like ta, bn, de, ko"}, 400)
                translate_ui(app.llm, lang, str(body.get("name") or ""))
                return self._json({"ok": True, "lang": lang, "languages": available()})
            if path == "/api/action/test-ai":
                answer = app.llm.text("You are terse.", "Reply with exactly: PixelPaw is ready", temperature=0)
                return self._json({"ok": True, "answer": answer[:120]})
            # ---- this computer only --------------------------------------
            if not is_loopback(self.client):
                return self._json({"ok": False, "error": "this can only be changed on the computer running PixelPaw"}, 403)
            return self._post_local(path, body)

        def _post_local(self, path: str, body: dict):
            from .config import update_env

            if path == "/api/setup/ai":
                provider = body.get("provider", "gemini")
                values = {"LLM_PROVIDER": "openai" if provider in ("openai", "local") else "gemini"}
                key = str(body.get("key") or "").strip()
                if provider == "gemini":
                    if key:
                        values["GEMINI_API_KEY"] = key
                    if body.get("model"):
                        values["GEMINI_MODEL"] = str(body["model"]).strip()
                elif provider == "openai":
                    if key:
                        values["OPENAI_API_KEY"] = key
                    values["OPENAI_BASE_URL"] = ""
                    if body.get("model"):
                        values["OPENAI_MODEL"] = str(body["model"]).strip()
                else:  # local, e.g. Ollama
                    values["OPENAI_BASE_URL"] = str(body.get("base_url") or "http://localhost:11434/v1").strip()
                    values["OPENAI_MODEL"] = str(body.get("model") or "llama3.2").strip()
                update_env(app.settings.env_file, values)
                app.reload()
                try:
                    answer = app.llm.text("You are terse.", "Reply with exactly: PixelPaw is ready", temperature=0)
                    return self._json({"ok": True, "answer": answer[:120]})
                except Exception as exc:
                    return self._json({"ok": False, "error": str(exc)[:300]})
            if path == "/api/setup/client-secret":
                from .youtube import validate_client_secret

                try:
                    data = validate_client_secret(str(body.get("content") or ""))
                except (ValueError, KeyError) as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                app.settings.client_secrets_file.write_text(json.dumps(data, indent=1), encoding="utf-8")
                return self._json({"ok": True})
            if path == "/api/setup/notify":
                values = {}
                for field, env in (("discord", "DISCORD_WEBHOOK_URL"), ("telegram_token", "TELEGRAM_BOT_TOKEN"),
                                   ("telegram_chat", "TELEGRAM_CHAT_ID")):
                    if field in body:
                        values[env] = str(body[field] or "").strip()
                update_env(app.settings.env_file, values)
                app.reload()
                return self._json({"ok": True})
            if path == "/api/security/rotate":
                state["token"] = rotate_token(app)
                return self._json({"ok": True}, extra=self._session_cookie())
            if path == "/api/security/lan":
                update_env(app.settings.env_file, {"PIXELPAW_WEB_HOST": "0.0.0.0" if body.get("enabled") else "127.0.0.1"})
                if service:
                    service.restart_web()
                return self._json({"ok": True, "restarting": bool(service)})
            if path == "/api/system/autostart":
                from . import autostart

                autostart.enable() if body.get("enabled") else autostart.disable()
                return self._json({"ok": True, "enabled": autostart.is_enabled()})
            if path == "/api/system/shortcut":
                from . import autostart

                return self._json({"ok": True, "path": str(autostart.create_desktop_shortcut())})
            if path == "/api/system/stop":
                if service:
                    threading.Timer(0.5, service.shutdown).start()
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

    return Handler


def start_web(app, service=None) -> ThreadingHTTPServer | None:
    host, port = app.settings.web_host, app.settings.web_port
    try:
        server = ThreadingHTTPServer((host, port), make_handler(app, service))
    except OSError as exc:
        log.error("Web server could not start on %s:%s (%s).", host, port, exc)
        health.fail("web", f"port {port} unavailable")
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    health.ok("web", f"http://127.0.0.1:{server.server_port}/")
    health.warn("overlay", "no overlay connected (add it in OBS)")
    log.info("🧭 PixelPaw app: http://127.0.0.1:%s/   🎥 OBS overlay: http://127.0.0.1:%s/overlay",
             server.server_port, server.server_port)
    if host in ("0.0.0.0", "::"):
        log.info("📱 Phone access is on (link in Settings). LAN address: http://%s:%s/", lan_ip(), server.server_port)
    return server
