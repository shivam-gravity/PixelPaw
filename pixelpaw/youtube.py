"""YouTube Data API v3 + Analytics API v2 over plain REST, with a built-in OAuth sign-in.

No Google SDKs. Sign-in uses the "installed app" loopback flow with PKCE; the setup page uses
the same flow with PixelPaw's own web server as the redirect target.

Reliability: transient failures are retried with exponential backoff + jitter. Reads retry on
network errors too; writes (replies, chat messages) only retry when Google clearly rejected the
request, so a reply can never be posted twice. Expired tokens refresh once automatically.
Every call is charged to the local quota tracker (live chat gets a reserve).
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import random
import secrets
import threading
import time
import urllib.parse
import webbrowser
from datetime import date

from .config import Settings
from .health import health
from .net import ApiError, request
from .quota import QuotaBudgetExceeded, is_live_path, youtube_cost

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
API = "https://www.googleapis.com/youtube/v3"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

READ_RETRY = {0, 429, 500, 502, 503, 504}
WRITE_RETRY = {429, 500, 502, 503}  # Google rejected it before doing anything: safe to resend
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


class NotAuthorised(RuntimeError):
    pass


def error_reason(exc: Exception) -> str:
    return getattr(exc, "reason", "") or ""


# ---- OAuth ------------------------------------------------------------------
def client_info(settings: Settings) -> dict:
    if not settings.client_secrets_file.exists():
        raise NotAuthorised("The Google OAuth client file (client_secret.json) hasn't been added yet.")
    data = json.loads(settings.client_secrets_file.read_text(encoding="utf-8"))
    info = data.get("installed") or data.get("web") or {}
    if not info.get("client_id"):
        raise NotAuthorised("client_secret.json is not an OAuth client file (create a 'Desktop app' client).")
    return info


def validate_client_secret(raw: str) -> dict:
    data = json.loads(raw)
    info = data.get("installed") or data.get("web") or {}
    if not info.get("client_id") or not info.get("client_secret"):
        raise ValueError("This isn't a Google OAuth client file. Download the JSON of a 'Desktop app' OAuth client.")
    return data


def oauth_begin(settings: Settings, redirect_uri: str) -> tuple[str, dict]:
    """Return (auth_url, pending) where pending must be passed to oauth_finish."""
    info = client_info(settings)
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": info["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    return url, {"state": state, "verifier": verifier, "redirect_uri": redirect_uri, "created": time.time()}


def oauth_finish(settings: Settings, code: str, pending: dict) -> dict:
    info = client_info(settings)
    tok = request("POST", info.get("token_uri", TOKEN_URI), form={
        "code": code, "client_id": info["client_id"], "client_secret": info.get("client_secret", ""),
        "redirect_uri": pending["redirect_uri"], "grant_type": "authorization_code", "code_verifier": pending["verifier"],
    })
    token = {
        "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
        "expiry": time.time() + int(tok.get("expires_in", 3600)), "scope": tok.get("scope", ""),
        "client_id": info["client_id"], "client_secret": info.get("client_secret", ""),
        "token_uri": info.get("token_uri", TOKEN_URI),
    }
    settings.token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text(json.dumps(token, indent=1), encoding="utf-8")
    health.ok("youtube", "signed in")
    return token


def sign_in(settings: Settings, open_browser: bool = True) -> dict:
    """Command-line sign-in: catches the redirect on a temporary 127.0.0.1 port."""
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            result.update(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query)))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2 style='font-family:system-ui;padding:2em'>🐾 Done. You can close this tab.</h2>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    url, pending = oauth_begin(settings, f"http://127.0.0.1:{server.server_port}/")
    print(f"Opening your browser to sign in. If it doesn't open, visit:\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    server.timeout = 1
    deadline = time.time() + 300
    while "code" not in result and "error" not in result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if result.get("state") != pending["state"] or "code" not in result:
        raise NotAuthorised(f"Sign-in did not complete: {result.get('error', 'timed out')}")
    return oauth_finish(settings, result["code"], pending)


class Tokens:
    """Holds the access token and refreshes it when needed (thread-safe)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        if not settings.token_file.exists():
            raise NotAuthorised("YouTube isn't connected yet. Open PixelPaw → Setup → Sign in with Google.")
        self.data = json.loads(settings.token_file.read_text(encoding="utf-8"))
        if "expiry" not in self.data or "client_id" not in self.data:
            raise NotAuthorised("Please sign in with Google again (Setup).")

    def bearer(self, force_refresh: bool = False) -> str:
        with self._lock:
            if force_refresh or time.time() > self.data["expiry"] - 60:
                self._refresh()
            return self.data["access_token"]

    def _refresh(self) -> None:
        if not self.data.get("refresh_token"):
            raise NotAuthorised("YouTube sign-in expired. Open PixelPaw → Setup → Sign in with Google.")
        try:
            tok = request("POST", self.data.get("token_uri", TOKEN_URI), form={
                "client_id": self.data["client_id"], "client_secret": self.data.get("client_secret", ""),
                "refresh_token": self.data["refresh_token"], "grant_type": "refresh_token",
            })
        except ApiError as exc:
            if exc.status in (400, 401):
                health.fail("youtube", "sign-in expired: reconnect in Setup")
                raise NotAuthorised("YouTube sign-in expired. Open PixelPaw → Setup → Sign in with Google.") from exc
            raise
        self.data["access_token"] = tok["access_token"]
        self.data["expiry"] = time.time() + int(tok.get("expires_in", 3600))
        self.settings.token_file.write_text(json.dumps(self.data, indent=1), encoding="utf-8")


# ---- API --------------------------------------------------------------------
class YouTube:
    def __init__(self, settings: Settings, quota=None, api: str = API, analytics: str = ANALYTICS,
                 max_attempts: int = 4, sleep=time.sleep):
        self.tokens = Tokens(settings)
        self.quota = quota
        self.api, self.analytics = api.rstrip("/"), analytics
        self.max_attempts, self._sleep = max_attempts, sleep
        self._channel: dict | None = None

    def _call(self, method: str, url: str, params: dict | None = None, body=None):
        path = urllib.parse.urlparse(url).path
        analytics = url.startswith(self.analytics)
        live = is_live_path(path)
        cost = 0 if analytics else youtube_cost(method, path)
        if self.quota and cost:
            self.quota.check(cost, live)
        retry_on = READ_RETRY if method in ("GET", "DELETE") else WRITE_RETRY
        refreshed, delay = False, 1.0
        for attempt in range(1, self.max_attempts + 1):
            headers = {"Authorization": f"Bearer {self.tokens.bearer(force_refresh=refreshed)}"}
            try:
                resp = request(method, url, params=params, json_body=body, headers=headers)
                if self.quota:
                    self.quota.charge(cost, live)
                health.ok("youtube", "connected")
                return resp
            except QuotaBudgetExceeded:
                raise
            except ApiError as exc:
                if exc.status and self.quota:
                    self.quota.charge(cost, live)  # Google bills rejected requests too
                if exc.reason == "quotaExceeded":
                    if self.quota:
                        self.quota.mark_exhausted()
                    health.fail("youtube", "daily YouTube quota used up (resets midnight Pacific)")
                    raise
                if exc.status == 401 and not refreshed:
                    refreshed = True
                    continue
                transient = exc.status in retry_on or (exc.status == 403 and exc.reason in RATE_REASONS)
                if transient and attempt < self.max_attempts:
                    wait = delay + random.uniform(0, delay / 2)
                    health.warn("youtube", f"retrying after {exc.status or 'network error'}")
                    log.warning("YouTube %s %s failed (%s), retry %d in %.1fs", method, path, exc, attempt, wait,
                                extra={"event": "youtube_retry", "status": exc.status, "path": path})
                    self._sleep(wait)
                    delay = min(delay * 2, 20)
                    continue
                if exc.status == 0 or exc.status >= 500:
                    health.fail("youtube", f"{exc.status or 'network'}: {exc.message[:120]}")
                raise
        raise ApiError(0, "retries", "gave up after retries")

    # ---- channel / videos ----------------------------------------------
    def channel(self, refresh: bool = False) -> dict:
        if self._channel is None or refresh:
            resp = self._call("GET", f"{self.api}/channels", {"part": "snippet,statistics,contentDetails", "mine": True})
            items = resp.get("items") or []
            if not items:
                raise RuntimeError("This Google account has no YouTube channel.")
            self._channel = items[0]
        return self._channel

    @property
    def channel_id(self) -> str:
        return self.channel()["id"]

    def videos(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        ids = [i for i in dict.fromkeys(ids) if i]
        for start in range(0, len(ids), 50):
            resp = self._call("GET", f"{self.api}/videos", {"part": "snippet,statistics", "id": ",".join(ids[start:start + 50])})
            for item in resp.get("items", []):
                out[item["id"]] = item
        return out

    def recent_uploads(self, n: int = 10) -> list[dict]:
        uploads = self.channel()["contentDetails"]["relatedPlaylists"]["uploads"]
        ids: list[str] = []
        token = None
        while len(ids) < n:
            resp = self._call("GET", f"{self.api}/playlistItems", {
                "part": "contentDetails", "playlistId": uploads, "maxResults": min(50, n - len(ids)), "pageToken": token})
            ids += [i["contentDetails"]["videoId"] for i in resp.get("items", [])]
            token = resp.get("nextPageToken")
            if not token:
                break
        by_id = self.videos(ids)
        return [by_id[i] for i in ids if i in by_id]

    # ---- comments -------------------------------------------------------
    def comment_threads(self, max_pages: int = 3):
        """Newest-first comment threads across all of the channel's videos."""
        token = None
        for _ in range(max_pages):
            resp = self._call("GET", f"{self.api}/commentThreads", {
                "part": "snippet,replies", "allThreadsRelatedToChannelId": self.channel_id, "order": "time",
                "maxResults": 100, "textFormat": "plainText", "pageToken": token})
            yield from resp.get("items", [])
            token = resp.get("nextPageToken")
            if not token:
                return

    def reply(self, parent_id: str, text: str) -> str:
        resp = self._call("POST", f"{self.api}/comments", {"part": "snippet"},
                          {"snippet": {"parentId": parent_id, "textOriginal": text}})
        return resp["id"]

    def set_moderation(self, comment_id: str, status: str) -> None:
        self._call("POST", f"{self.api}/comments/setModerationStatus", {"id": comment_id, "moderationStatus": status})

    # ---- analytics ------------------------------------------------------
    def daily_analytics(self, start: date, end: date) -> list[dict]:
        resp = self._call("GET", self.analytics, {
            "ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
            "metrics": "views,estimatedMinutesWatched,subscribersGained,subscribersLost,likes,comments",
            "dimensions": "day", "sort": "day"})
        headers = [h["name"] for h in resp.get("columnHeaders", [])]
        return [dict(zip(headers, row)) for row in resp.get("rows", []) or []]

    # ---- live chat ------------------------------------------------------
    def active_live_chat(self, video_id: str | None = None) -> tuple[str, str] | None:
        """Return (live_chat_id, title) of the active stream, or None."""
        if video_id:
            resp = self._call("GET", f"{self.api}/videos", {"part": "snippet,liveStreamingDetails", "id": video_id})
            for item in resp.get("items", []):
                chat = item.get("liveStreamingDetails", {}).get("activeLiveChatId")
                if chat:
                    return chat, item["snippet"]["title"]
            return None
        resp = self._call("GET", f"{self.api}/liveBroadcasts", {
            "part": "snippet", "broadcastStatus": "active", "broadcastType": "all", "maxResults": 5})
        for item in resp.get("items", []):
            chat = item["snippet"].get("liveChatId")
            if chat:
                return chat, item["snippet"]["title"]
        return None

    def live_messages(self, chat_id: str, page_token: str | None = None) -> dict:
        return self._call("GET", f"{self.api}/liveChat/messages", {
            "liveChatId": chat_id, "part": "snippet,authorDetails", "pageToken": page_token, "maxResults": 2000})

    def live_send(self, chat_id: str, text: str) -> None:
        self._call("POST", f"{self.api}/liveChat/messages", {"part": "snippet"}, {"snippet": {
            "liveChatId": chat_id, "type": "textMessageEvent", "textMessageDetails": {"messageText": text[:200]}}})

    def live_delete(self, message_id: str) -> None:
        self._call("DELETE", f"{self.api}/liveChat/messages", {"id": message_id})
