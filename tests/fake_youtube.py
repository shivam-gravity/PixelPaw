"""A small in-memory fake of the YouTube Data/Analytics APIs + Google's token endpoint.

Lets the tests exercise PixelPaw's real REST client (retries, token refresh, quota, live chat)
without network access. Faults can be injected per path: fake.fail("/commentThreads", 503, times=2).
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ME = "UCme"


def ts(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeYouTube:
    def __init__(self):
        self.lock = threading.Lock()
        self.threads: list[dict] = []
        self.replies: list[tuple[str, str]] = []
        self.moderated: list[tuple[str, str]] = []
        self.chat_queue: list[dict] = []
        self.chat_sent: list[str] = []
        self.chat_deleted: list[str] = []
        self.live = False
        self.faults: dict[str, list[tuple[int, str]]] = defaultdict(list)
        self.calls: list[tuple[str, str]] = []
        self.token_refreshes = 0
        self.valid_tokens = {"good-token"}
        self.token_counter = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    # ---- test helpers ----------------------------------------------------
    def add_comment(self, cid: str, text: str, author: str = "Viewer", hours_ago: float = 1, author_id: str = "UCv"):
        self.threads.insert(0, {"snippet": {"videoId": "v1", "canReply": True, "topLevelComment": {"id": cid, "snippet": {
            "authorDisplayName": author, "authorChannelId": {"value": author_id}, "textOriginal": text,
            "publishedAt": ts(hours_ago)}}}, "replies": {"comments": []}})

    def chat(self, text: str, name: str = "Ana", channel: str = "UCana", kind: str = "textMessageEvent", **extra):
        mid = f"m{len(self.chat_queue) + len(self.chat_sent) + 1000}"
        self.chat_queue.append({"id": mid, "snippet": {"type": kind, "displayMessage": text, **extra},
                                "authorDetails": {"displayName": name, "channelId": channel}})
        return mid

    def fail(self, path_suffix: str, status: int, reason: str = "backendError", times: int = 1):
        self.faults[path_suffix] += [(status, reason)] * times

    def expire_tokens(self):
        self.valid_tokens.clear()

    def stop(self):
        self.server.shutdown()

    # ---- server ------------------------------------------------------------
    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, data=None):
                body = b"" if data is None else json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _err(self, code, reason, message="fake error"):
                self._json(code, {"error": {"code": code, "message": message, "errors": [{"reason": reason}]}})

            def _route(self, method):
                u = urllib.parse.urlparse(self.path)
                path, q = u.path, dict(urllib.parse.parse_qsl(u.query))
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                with fake.lock:
                    fake.calls.append((method, path))
                if path == "/token":
                    form = dict(urllib.parse.parse_qsl(raw.decode()))
                    with fake.lock:
                        fake.token_refreshes += form.get("grant_type") == "refresh_token"
                        fake.token_counter += 1
                        tok = f"tok-{fake.token_counter}"
                        fake.valid_tokens.add(tok)
                    return self._json(200, {"access_token": tok, "expires_in": 3600, "refresh_token": "r1"})
                if path == "/v1/chat/completions":  # fake OpenAI-compatible local model (setup test)
                    return self._json(200, {"choices": [{"message": {"content": "PixelPaw is ready"}}]})
                auth = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if auth not in fake.valid_tokens:
                    return self._err(401, "authError", "token expired")
                for suffix, queue in fake.faults.items():
                    if path.endswith(suffix) and queue:
                        status, reason = queue.pop(0)
                        return self._err(status, reason)
                body = json.loads(raw) if raw else {}
                p = path.split("/youtube/v3", 1)[-1]
                if path.endswith("/analytics"):
                    return self._json(200, {"columnHeaders": [{"name": "day"}, {"name": "views"}], "rows": [["2026-11-01", 10]]})
                if p == "/channels":
                    return self._json(200, {"items": [{"id": ME, "snippet": {"title": "Fake Channel", "customUrl": "@fake", "description": "Python"},
                                                       "statistics": {"subscriberCount": "100", "viewCount": "1000", "videoCount": "3"},
                                                       "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
                if p == "/videos":
                    ids = [i for i in q.get("id", "").split(",") if i]
                    return self._json(200, {"items": [{"id": i, "snippet": {"title": f"Video {i}", "publishedAt": ts(5)},
                                                       "statistics": {"viewCount": "10", "likeCount": "1", "commentCount": "1"}} for i in ids]})
                if p == "/playlistItems":
                    return self._json(200, {"items": [{"contentDetails": {"videoId": "v1"}}]})
                if p == "/commentThreads":
                    return self._json(200, {"items": list(fake.threads)})
                if p == "/comments" and method == "POST":
                    parent, text = body["snippet"]["parentId"], body["snippet"]["textOriginal"]
                    with fake.lock:
                        fake.replies.append((parent, text))
                    return self._json(200, {"id": f"r-{parent}"})
                if p == "/comments/setModerationStatus":
                    fake.moderated.append((q["id"], q["moderationStatus"]))
                    return self._json(204)
                if p == "/liveBroadcasts":
                    items = [{"snippet": {"liveChatId": "chat1", "title": "Fake Stream"}}] if fake.live else []
                    return self._json(200, {"items": items})
                if p == "/liveChat/messages":
                    if method == "GET":
                        items = []
                        if q.get("pageToken"):  # the first page is old backlog: the bot skips it anyway
                            with fake.lock:
                                items, fake.chat_queue = list(fake.chat_queue), []
                        token = int(q.get("pageToken") or 0) + 1
                        return self._json(200, {"items": items, "nextPageToken": str(token), "pollingIntervalMillis": 50})
                    if method == "POST":
                        fake.chat_sent.append(body["snippet"]["textMessageDetails"]["messageText"])
                        return self._json(200, {"id": "sent"})
                    if method == "DELETE":
                        fake.chat_deleted.append(q["id"])
                        return self._json(204)
                return self._err(404, "notFound")

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_DELETE(self):
                self._route("DELETE")

        return H
