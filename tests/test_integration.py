"""Integration tests: PixelPaw's real REST client, live chat, service, web security and setup
against a fake YouTube API (tests/fake_youtube.py). No network, no credentials.

Run:  python tests/test_integration.py
"""
import http.client
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fake_youtube import FakeYouTube  # noqa: E402

from pixelpaw import comments, livechat  # noqa: E402
from pixelpaw.app import App  # noqa: E402
from pixelpaw.config import load_settings  # noqa: E402
from pixelpaw.health import health, setup_logging  # noqa: E402
from pixelpaw.net import ApiError  # noqa: E402
from pixelpaw.quota import QuotaBudgetExceeded, QuotaTracker  # noqa: E402
from pixelpaw.service import Job  # noqa: E402
from pixelpaw.store import Store  # noqa: E402
from pixelpaw.web import is_allowed_client, is_allowed_host, start_web, web_token  # noqa: E402
from pixelpaw.youtube import YouTube  # noqa: E402

PASSED = []


def ok(name):
    PASSED.append(name)
    print(f"✔ {name}")


tmp = Path(tempfile.mkdtemp())
fake = FakeYouTube()
log_file = setup_logging(tmp / "logs")
logging.getLogger().handlers = [h for h in logging.getLogger().handlers if not isinstance(h, logging.StreamHandler)
                                or isinstance(h, logging.FileHandler)]  # keep test output quiet

(tmp / "client_secret.json").write_text(json.dumps({"installed": {
    "client_id": "cid", "client_secret": "csecret", "token_uri": fake.base + "/token"}}), encoding="utf-8")
(tmp / "token.json").write_text(json.dumps({
    "access_token": "good-token", "refresh_token": "r1", "expiry": time.time() + 3600,
    "client_id": "cid", "client_secret": "csecret", "token_uri": fake.base + "/token"}), encoding="utf-8")
(tmp / ".env").write_text("# test env\n", encoding="utf-8")

settings = replace(load_settings(), client_secrets_file=tmp / "client_secret.json", token_file=tmp / "token.json",
                   db_path=tmp / "data" / "db.sqlite3", reports_dir=tmp / "reports", output_dir=tmp / "out",
                   persona_file=ROOT / "persona.yaml", env_file=tmp / ".env", data_dir=tmp / "data",
                   discord_webhook_url="", telegram_bot_token="", web_port=0, web_host="127.0.0.1",
                   llm_provider="gemini", gemini_api_key="test")
sleeps = []


class FakeLLM:
    def __init__(self):
        self.text_calls = 0

    def json(self, system, prompt, temperature=0.4):
        if "Trust & Safety" in system:
            items = json.loads(prompt[prompt.index("Messages:") + 9:])
            return {"items": [{"id": i["id"], "compliant": True, "issues": []} for i in items]}
        if "Analyse these comments" in prompt:
            payload = json.loads(prompt[prompt.index("Comments:") + 9:])
            out = []
            for c in payload:
                q = "?" in c["text"]
                out.append({"id": c["id"], "category": "question" if q else "praise", "sentiment": "positive",
                            "policy_flag": "none", "needs_human": False,
                            "reply": f"Great question about {c['text'][:20]}, details are in the description!" if q
                            else f"Thank you so much, glad you enjoyed {c['id']}!"})
            return {"items": out}
        return {}

    def text(self, system, prompt, temperature=0.7):
        self.text_calls += 1
        return f"Answer number {self.text_calls}: I use a Fifine mic"


def make_app(name="main"):
    app = App(replace(settings, db_path=tmp / name / "db.sqlite3", data_dir=tmp / name))
    app.persona["comments"].update(mode="auto", reply_gap_seconds=0)
    app.persona["links"].update(github="https://github.com/me", discord="https://discord.gg/x")
    app.yt_factory = lambda: YouTube(app.settings, quota=app.quota, api=fake.base + "/youtube/v3",
                                     analytics=fake.base + "/analytics", sleep=sleeps.append)
    app._llm = FakeLLM()
    return app


app = make_app()

# ---- REST client: quota charging, retries, token refresh ------------------------------------
app.yt.channel()
assert app.quota.used()["normal"] == 1, app.quota.used()
ok("REST client reads channel; quota charged 1 unit")

fake.add_comment("c1", "What mic do you use?")
fake.add_comment("c2", "Loved this video")
fake.fail("/commentThreads", 503, times=2)
fake.fail("/comments", 503, times=1)  # first reply POST rejected -> safe to retry
before = len(sleeps)
st = comments.sweep(app)
assert st["replied"] == 2, st
assert sorted(p for p, _ in fake.replies) == ["c1", "c2"] and len(fake.replies) == 2, fake.replies  # no duplicates
assert len(sleeps) - before == 3, sleeps  # 2 read retries + 1 write retry, with backoff
assert sleeps[-2] >= 2.0, sleeps  # exponential backoff
ok("503s on reads and on a rejected write are retried with backoff; replies posted exactly once")

fake.expire_tokens()
app.yt.channel(refresh=True)
assert fake.token_refreshes == 1
ok("expired access token refreshed automatically (401 → refresh → retry)")

fake.fail("/videos", 403, "rateLimitExceeded")
assert app.yt.videos(["v1"])
ok("403 rateLimitExceeded treated as transient and retried")

# Network errors: reads retry, writes don't (a lost response must never cause a double post).
dead = YouTube(settings, quota=None, api="http://127.0.0.1:1/youtube/v3", sleep=sleeps.append)
n = len(sleeps)
try:
    dead.reply("c9", "hi")
except ApiError as e:
    assert e.status == 0
assert len(sleeps) == n, "write must not be retried after a network error"
try:
    dead.videos(["x"])
except ApiError:
    pass
assert len(sleeps) == n + 3, "reads retry 3 times"
ok("network errors: reads retried, writes never retried (no double posting)")

fake.fail("/videos", 403, "quotaExceeded")
try:
    app.yt.videos(["v1"])
    raise AssertionError("expected quotaExceeded")
except ApiError as e:
    assert e.reason == "quotaExceeded"
assert app.quota.exhausted() and health.status("youtube") == "error"
calls = len(fake.calls)
try:
    app.yt.channel(refresh=True)
    raise AssertionError("expected local block")
except QuotaBudgetExceeded:
    pass
assert len(fake.calls) == calls, "no HTTP call once quota is exhausted"
app.store.kv_set("quota_exhausted_day", "")
ok("quotaExceeded marks the day exhausted and blocks further calls locally")

# ---- quota budget: live chat reserve -----------------------------------------------------------
qstore = Store(tmp / "quota.sqlite3")
tracker = QuotaTracker(qstore, daily_limit=200, live_reserve=150)
tracker.charge(49, live=False)
yq = YouTube(settings, quota=tracker, api=fake.base + "/youtube/v3", sleep=sleeps.append)
yq.channel()  # 50/50 normal budget
try:
    yq.channel(refresh=True)
    raise AssertionError("normal work should be stopped by the live reserve")
except QuotaBudgetExceeded:
    pass
assert yq.active_live_chat() is None  # live calls may still use the reserve
assert tracker.used()["live"] == 1
tracker.charge(140, live=True)
assert tracker.poll_multiplier() > 1
ok("quota: normal work stops at the live-chat reserve; live chat keeps working; polling slows when low")

# ---- live chat end-to-end ------------------------------------------------------------------------
lc = app.persona["live_chat"]
lc.update(ask_cooldown_seconds=0, ask_per_user_per_hour=2, min_seconds_between_bot_messages=0, poll_seconds_min=0.05,
          command_cooldown_seconds=30, greeting="")
app.persona["faq"] = [{"q": "Where is the source code?", "a": "It's on my GitHub: https://github.com/me"}]
fake.live = True
watcher = livechat.LiveWatcher(app)
watcher.check()
time.sleep(0.4)
llm = app._llm
fake.chat("!discord")
fake.chat("!ask where is the source code")
spam_id = fake.chat("check www.spam.xyz now")
fake.chat("!ask what mic do you use?", name="Ana", channel="UCana")
time.sleep(0.6)
fake.chat("!ask What mic do you use", name="Bob", channel="UCbob")
for i in range(3):
    fake.chat(f"!ask question number {i} about cameras", name="Zed", channel="UCzed")
time.sleep(1.0)
watcher.shutdown()
watcher.thread.join(3)
sent = fake.chat_sent
assert "Join the community: https://discord.gg/x" in sent, sent
assert any("It's on my GitHub" in s for s in sent), sent  # FAQ answer, no AI call
assert spam_id in fake.chat_deleted
assert any(s.startswith("🤖 @Bob answered just above") for s in sent), sent  # cache hit, no repetition
zed = [s for s in sent if "@Zed" in s]
assert len(zed) == 2, zed  # per-viewer limit (2/hour)
assert llm.text_calls == 3, llm.text_calls  # Ana + 2× Zed; FAQ and cache were free
assert app.ai_budget.status()["cached"] >= 2
ok("live chat: FAQ first, cache second, AI last; per-viewer limit; link spam deleted; no repetitive answers")

# Watcher restarts a crashed bot.
orig = livechat.LiveChatBot.run
livechat.LiveChatBot.run = lambda self, stop=None: (_ for _ in ()).throw(RuntimeError("boom"))
w2 = livechat.LiveWatcher(app)
w2.check()
w2.thread.join(2)
assert health.status("live_chat") == "error"
livechat.LiveChatBot.run = orig
w2.check()
time.sleep(0.2)
assert w2.thread.is_alive()
w2.shutdown()
w2.thread.join(3)
fake.live = False
ok("live chat watcher restarts the bot after a crash")

# ---- service job backoff ---------------------------------------------------------------------------
from zoneinfo import ZoneInfo  # noqa: E402

state = {"fail": True}


def flaky():
    if state["fail"]:
        raise RuntimeError("temporary")


job = Job("flaky", flaky, every_minutes=60, run_now=True, tz=ZoneInfo("UTC"))
job.run()
from datetime import datetime, timedelta  # noqa: E402

assert job.failures == 1 and job.next_run < datetime.now(ZoneInfo("UTC")) + timedelta(minutes=2)
assert health.status("job:flaky") == "error"
state["fail"] = False
job.run()
assert job.failures == 0 and health.status("job:flaky") == "ok"
ok("failed routines retry with backoff and recover")

# ---- web security --------------------------------------------------------------------------------
assert not is_allowed_client("8.8.8.8") and is_allowed_client("192.168.1.5") and is_allowed_client("::1")
assert is_allowed_client("::ffff:127.0.0.1") and not is_allowed_client("::ffff:8.8.8.8")
assert is_allowed_host("127.0.0.1:8765") and is_allowed_host("localhost") and is_allowed_host("192.168.0.10:8765")
assert not is_allowed_host("evil.com") and not is_allowed_host("pixelpaw.attacker.net:8765")
ok("network filters: only local/private clients and hosts")

web = start_web(app)
port = web.server_port
HOST = f"127.0.0.1:{port}"


def req(method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": HOST, **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    out = (r.status, dict(r.getheaders()), raw)
    c.close()
    return out


status, headers, _ = req("GET", "/")
cookie = headers.get("Set-Cookie", "").split(";")[0]
assert status == 200 and cookie.startswith("pp_session=") and "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
assert "frame-ancestors 'none'" in headers.get("Content-Security-Policy", "")
assert web_token(app) not in cookie  # cookie is an HMAC, not the token
status, headers, _ = req("GET", "/", {"Sec-Fetch-Site": "cross-site"})
assert "Set-Cookie" not in headers
ok("same-computer visit signs in with an HttpOnly SameSite cookie; cross-site visits don't")

C = {"Cookie": cookie}
ORIGIN = {"Origin": f"http://{HOST}"}
assert req("GET", "/api/state")[0] == 401
assert req("GET", "/api/state", C)[0] == 200
assert req("POST", "/api/test", {**C, **ORIGIN}, {})[0] == 200
assert req("POST", "/api/test", {**C, "Origin": "http://evil.com"}, {})[0] == 401
assert req("POST", "/api/test", C, {})[0] == 401  # no Origin -> refused
assert req("GET", "/", {"Host": "evil.com"})[0] == 403
assert json.loads(req("GET", "/healthz")[2])["app"] == "pixelpaw"
ok("API needs the session; writes need same Origin (CSRF); DNS-rebinding Host refused")

# setup endpoints
status, _, raw = req("POST", "/api/setup/client-secret", {**C, **ORIGIN}, {"content": "{\"not\": \"oauth\"}"})
assert status == 400
good = json.dumps({"installed": {"client_id": "cid", "client_secret": "csecret", "token_uri": fake.base + "/token"}})
assert json.loads(req("POST", "/api/setup/client-secret", {**C, **ORIGIN}, {"content": good})[2])["ok"]
r = json.loads(req("POST", "/api/setup/ai", {**C, **ORIGIN}, {"provider": "local", "base_url": fake.base + "/v1", "model": "tiny"})[2])
assert r["ok"] and "ready" in r["answer"], r
env_text = (tmp / ".env").read_text(encoding="utf-8")
assert "LLM_PROVIDER=openai" in env_text and "OPENAI_BASE_URL=" + fake.base + "/v1" in env_text
app._llm = FakeLLM()
ok("setup: Google client file validated and saved; AI provider saved to .env and tested")

# Google sign-in through the dashboard
status, headers, _ = req("GET", "/oauth/start", C)
loc = headers["Location"]
assert status == 303 and loc.startswith("https://accounts.google.com/") and "code_challenge=" in loc
state_param = dict(p.split("=", 1) for p in loc.split("?", 1)[1].split("&"))["state"]
(tmp / "main" / "token.json").unlink(missing_ok=True)
status, headers, _ = req("GET", f"/oauth/callback?state={state_param}&code=abc")
assert status == 303 and headers["Location"] == "/#setup?youtube=ok", headers
assert json.loads((tmp / "token.json").read_text())["refresh_token"] == "r1"
assert req("GET", "/oauth/callback?state=forged&code=abc")[1]["Location"].startswith("/#setup?error=")
ok("Sign in with Google from the setup page (PKCE + state checked; forged state refused)")

# settings
cur_cmds = set(app.persona["live_chat"]["commands"])
r = json.loads(req("POST", "/api/settings", {**C, **ORIGIN}, {"comments": {"mode": "review"}, "live_chat": {"commands": {"!new": "hello"}},
                                                              "ui": {"overlay": {"position": "nowhere", "seconds": 999}}, "bogus": {"x": 1}})[2])
assert r["ok"] and app.persona["comments"]["mode"] == "review"
assert app.persona["live_chat"]["commands"]["!new"] == "hello"
assert all(app.persona["live_chat"]["commands"][k] == "" for k in cur_cmds - {"!new"})
assert app.persona["ui"]["overlay"]["position"] != "nowhere" and app.persona["ui"]["overlay"]["seconds"] == 60
assert "bogus" not in json.loads((tmp / "main" / "persona.local.json").read_text())
ok("settings saved from the dashboard: validated, whitelisted, removed commands really removed")

# health + structured logs
h = json.loads(req("GET", "/api/health", C)[2])
assert {"youtube", "ai", "live_chat", "web"} <= set(h["components"]) and h["errors"] and "quota" in h
lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
assert any(line.get("event") == "youtube_retry" for line in lines), "structured retry events in the JSON log"
assert all({"ts", "level", "logger", "msg"} <= set(line) for line in lines)
ok("health endpoint + JSON-lines log with structured fields")

web.shutdown()

# lockout (fresh server so the counter starts at 0)
web2 = start_web(app)
port = web2.server_port
HOST = f"127.0.0.1:{port}"
codes = [req("GET", "/api/state", {"X-PixelPaw-Token": "wrong"})[0] for _ in range(21)]
assert codes[:20] == [401] * 20 and codes[20] == 429, codes
web2.shutdown()
ok("repeated wrong tokens lock the client out")

for k in ("LLM_PROVIDER", "OPENAI_BASE_URL", "OPENAI_MODEL"):
    os.environ.pop(k, None)
fake.stop()
print(f"\nALL {len(PASSED)} INTEGRATION TESTS PASSED")
