"""YouTube Data API v3 + Analytics API v2 over plain REST, with a built-in OAuth sign-in.

No Google SDKs: sign-in uses the standard "installed app" loopback flow with PKCE.

Quota notes (default 10,000 units/day): list calls ~1 unit, liveChatMessages.list ~5,
comments.insert / setModerationStatus / liveChatMessages.insert / delete ~50 each.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from datetime import date

from .config import Settings
from .net import ApiError, request

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
API = "https://www.googleapis.com/youtube/v3"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class NotAuthorised(RuntimeError):
    pass


def error_reason(exc: Exception) -> str:
    return getattr(exc, "reason", "") or ""


# ---- OAuth ------------------------------------------------------------------
def _client(settings: Settings) -> dict:
    if not settings.client_secrets_file.exists():
        raise NotAuthorised(
            f"OAuth client file not found: {settings.client_secrets_file}\n"
            "Download it from Google Cloud Console (see README, step 2)."
        )
    data = json.loads(settings.client_secrets_file.read_text(encoding="utf-8"))
    info = data.get("installed") or data.get("web") or {}
    if not info.get("client_id"):
        raise NotAuthorised("client_secret.json is not an OAuth client file (expected an 'installed' Desktop app client).")
    return info


def sign_in(settings: Settings, open_browser: bool = True) -> dict:
    """Interactive sign-in. Opens the browser, catches the redirect on 127.0.0.1, saves the token."""
    info = _client(settings)
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                result.update(q)
            ok = "code" in q and q.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "🐾 PixelPaw is signed in. You can close this tab." if ok else "Sign-in failed or was cancelled."
            self.wfile.write(f"<html><body style='font-family:system-ui;padding:3em'><h2>{msg}</h2></body></html>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": info["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    print(f"Opening your browser to sign in. If it doesn't open, visit:\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    server.timeout = 1
    deadline = time.time() + 300
    while not result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        raise NotAuthorised(f"Sign-in did not complete: {result.get('error', 'timed out')}")

    tok = request("POST", info.get("token_uri", TOKEN_URI), form={
        "code": result["code"], "client_id": info["client_id"], "client_secret": info.get("client_secret", ""),
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    })
    token = {
        "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
        "expiry": time.time() + int(tok.get("expires_in", 3600)), "scope": tok.get("scope", ""),
        "client_id": info["client_id"], "client_secret": info.get("client_secret", ""),
        "token_uri": info.get("token_uri", TOKEN_URI),
    }
    settings.token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text(json.dumps(token, indent=1), encoding="utf-8")
    return token


class Tokens:
    """Holds the access token and refreshes it when needed (thread-safe)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        if not settings.token_file.exists():
            raise NotAuthorised("Not signed in to YouTube. Run:  python -m pixelpaw auth")
        self.data = json.loads(settings.token_file.read_text(encoding="utf-8"))
        if "expiry" not in self.data or "client_id" not in self.data:
            raise NotAuthorised("Old or unknown token format. Run:  python -m pixelpaw auth")

    def bearer(self, force_refresh: bool = False) -> str:
        with self._lock:
            if force_refresh or time.time() > self.data["expiry"] - 60:
                self._refresh()
            return self.data["access_token"]

    def _refresh(self) -> None:
        if not self.data.get("refresh_token"):
            raise NotAuthorised("YouTube sign-in expired. Run:  python -m pixelpaw auth")
        try:
            tok = request("POST", self.data.get("token_uri", TOKEN_URI), form={
                "client_id": self.data["client_id"], "client_secret": self.data.get("client_secret", ""),
                "refresh_token": self.data["refresh_token"], "grant_type": "refresh_token",
            })
        except ApiError as exc:
            if exc.status in (400, 401):
                raise NotAuthorised(f"YouTube sign-in expired ({exc.reason}). Run:  python -m pixelpaw auth") from exc
            raise
        self.data["access_token"] = tok["access_token"]
        self.data["expiry"] = time.time() + int(tok.get("expires_in", 3600))
        self.settings.token_file.write_text(json.dumps(self.data, indent=1), encoding="utf-8")


# ---- API --------------------------------------------------------------------
class YouTube:
    def __init__(self, settings: Settings):
        self.tokens = Tokens(settings)
        self._channel: dict | None = None

    def _call(self, method: str, url: str, params: dict | None = None, body=None):
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self.tokens.bearer(force_refresh=attempt > 0)}"}
            try:
                return request(method, url, params=params, json_body=body, headers=headers)
            except ApiError as exc:
                if exc.status == 401 and attempt == 0:
                    continue
                raise
        return {}

    # ---- channel / videos ----------------------------------------------
    def channel(self, refresh: bool = False) -> dict:
        if self._channel is None or refresh:
            resp = self._call("GET", f"{API}/channels", {"part": "snippet,statistics,contentDetails", "mine": True})
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
            resp = self._call("GET", f"{API}/videos", {"part": "snippet,statistics", "id": ",".join(ids[start:start + 50])})
            for item in resp.get("items", []):
                out[item["id"]] = item
        return out

    def recent_uploads(self, n: int = 10) -> list[dict]:
        uploads = self.channel()["contentDetails"]["relatedPlaylists"]["uploads"]
        ids: list[str] = []
        token = None
        while len(ids) < n:
            resp = self._call("GET", f"{API}/playlistItems", {
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
            resp = self._call("GET", f"{API}/commentThreads", {
                "part": "snippet,replies", "allThreadsRelatedToChannelId": self.channel_id, "order": "time",
                "maxResults": 100, "textFormat": "plainText", "pageToken": token})
            yield from resp.get("items", [])
            token = resp.get("nextPageToken")
            if not token:
                return

    def reply(self, parent_id: str, text: str) -> str:
        resp = self._call("POST", f"{API}/comments", {"part": "snippet"},
                          {"snippet": {"parentId": parent_id, "textOriginal": text}})
        return resp["id"]

    def set_moderation(self, comment_id: str, status: str) -> None:
        self._call("POST", f"{API}/comments/setModerationStatus", {"id": comment_id, "moderationStatus": status})

    # ---- analytics ------------------------------------------------------
    def daily_analytics(self, start: date, end: date) -> list[dict]:
        resp = self._call("GET", ANALYTICS, {
            "ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
            "metrics": "views,estimatedMinutesWatched,subscribersGained,subscribersLost,likes,comments",
            "dimensions": "day", "sort": "day"})
        headers = [h["name"] for h in resp.get("columnHeaders", [])]
        return [dict(zip(headers, row)) for row in resp.get("rows", []) or []]

    # ---- live chat ------------------------------------------------------
    def active_live_chat(self, video_id: str | None = None) -> tuple[str, str] | None:
        """Return (live_chat_id, title) of the active stream, or None."""
        if video_id:
            resp = self._call("GET", f"{API}/videos", {"part": "snippet,liveStreamingDetails", "id": video_id})
            for item in resp.get("items", []):
                chat = item.get("liveStreamingDetails", {}).get("activeLiveChatId")
                if chat:
                    return chat, item["snippet"]["title"]
            return None
        resp = self._call("GET", f"{API}/liveBroadcasts", {
            "part": "snippet", "broadcastStatus": "active", "broadcastType": "all", "maxResults": 5})
        for item in resp.get("items", []):
            chat = item["snippet"].get("liveChatId")
            if chat:
                return chat, item["snippet"]["title"]
        return None

    def live_messages(self, chat_id: str, page_token: str | None = None) -> dict:
        return self._call("GET", f"{API}/liveChat/messages", {
            "liveChatId": chat_id, "part": "snippet,authorDetails", "pageToken": page_token, "maxResults": 2000})

    def live_send(self, chat_id: str, text: str) -> None:
        self._call("POST", f"{API}/liveChat/messages", {"part": "snippet"}, {"snippet": {
            "liveChatId": chat_id, "type": "textMessageEvent", "textMessageDetails": {"messageText": text[:200]}}})

    def live_delete(self, message_id: str) -> None:
        self._call("DELETE", f"{API}/liveChat/messages", {"id": message_id})
