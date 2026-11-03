"""Offline tests: fake YouTube + fake AI, no network, no credentials.

Run:  python tests/test_offline.py
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pixelpaw import comments, content, i18n, livechat, policy, report  # noqa: E402
from pixelpaw.app import App  # noqa: E402
from pixelpaw.config import load_dotenv, load_settings  # noqa: E402
from pixelpaw.events import bus  # noqa: E402
from pixelpaw.llm import LLMError  # noqa: E402
from pixelpaw.net import ApiError, request  # noqa: E402
from pixelpaw.scheduler import Job  # noqa: E402
from pixelpaw.web import start_web, web_token  # noqa: E402

tmp = Path(tempfile.mkdtemp())
base = replace(load_settings(), reports_dir=tmp / "reports", output_dir=tmp / "out", db_path=tmp / "db.sqlite3",
               persona_file=ROOT / "persona.yaml", discord_webhook_url="", telegram_bot_token="", web_port=0)
now = datetime.now(timezone.utc)
ts = lambda h: (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
ME = "UCme"
PASSED = []


def ok(name):
    PASSED.append(name)
    print(f"✔ {name}")


def thread(cid, text, hours=1, author_id="UCv"):
    return {"snippet": {"videoId": "v1", "canReply": True, "topLevelComment": {"id": cid, "snippet": {
        "authorDisplayName": "Viewer", "authorChannelId": {"value": author_id}, "textOriginal": text,
        "publishedAt": ts(hours)}}}, "replies": {"comments": []}}


# id: (category, sentiment, policy_flag, reply)
CANNED = {
    "c1": ("question", "neutral", "none", "I use a Fifine K669, budget king!"),
    "c2": ("praise", "positive", "none", "So glad it helped!"),
    "c3": ("collab", "positive", "none", ""),
    "c4": ("spam", "neutral", "scam_spam", ""),
    "c5": ("feedback", "negative", "none", "Thanks for flagging, fixing it now!"),
    "c10": ("other", "negative", "harassment", ""),
    "c11": ("other", "negative", "self_harm", ""),
    "c12": ("question", "neutral", "none", "Grab it here: https://random-deals.xyz/mic"),
    "c13": ("question", "neutral", "none", "Use code SAVE10 for crypto signals, guaranteed profit!"),
    "c14": ("praise", "positive", "none", "Thank you so much, that honestly means a lot to me!!"),
    "c15": ("praise", "positive", "none", "Thank you so much, that honestly means a lot to me!"),
    "c16": ("question", "neutral", "none", "Yes, the code is on my GitHub, check the description."),
    "c17": ("praise", "positive", "none", "¡Muchas gracias, me alegra que te sirviera!"),
}
TEXTS = {k: f"comment {k}" for k in CANNED}
TEXTS["c17"] = "¡Me encantó el video!"


class FakeYT:
    channel_id = ME

    def __init__(self):
        self.posted, self.moderated, self.sent, self.deleted = [], [], [], []
        self.threads = [thread(k, v) for k, v in TEXTS.items()] + [
            thread("own", "mine", author_id=ME), thread("short", "ok"), thread("old", "ancient", hours=500)]

    def channel(self, refresh=False):
        return {"id": ME, "snippet": {"title": "Test Channel", "customUrl": "@test", "description": "Python"},
                "statistics": {"subscriberCount": "1234", "viewCount": "56789", "videoCount": "42"},
                "contentDetails": {"relatedPlaylists": {"uploads": "UU"}}}

    def comment_threads(self, max_pages=3):
        yield from self.threads

    def videos(self, ids):
        return {i: {"id": i, "snippet": {"title": f"Video {i}", "publishedAt": ts(10)}, "statistics": {"viewCount": "100"}} for i in ids}

    def recent_uploads(self, n=10):
        return [{"id": "v1", "snippet": {"title": "Video v1", "publishedAt": ts(10)},
                 "statistics": {"viewCount": "100", "likeCount": "9", "commentCount": "5"}}]

    def reply(self, parent, text):
        self.posted.append((parent, text))
        return "r-" + parent

    def set_moderation(self, cid, status):
        self.moderated.append((cid, status))

    def daily_analytics(self, s, e):
        return [{"day": "2026-11-01", "views": 120, "estimatedMinutesWatched": 300, "subscribersGained": 4, "subscribersLost": 1}]

    def live_send(self, chat, text):
        self.sent.append(text)

    def live_delete(self, mid):
        self.deleted.append(mid)


class FakeLLM:
    def __init__(self, review_fails=False, flag_text=None):
        self.review_fails, self.flag_text = review_fails, flag_text

    def json(self, system, prompt, temperature=0.4):
        if "Trust & Safety" in system:
            if self.review_fails:
                raise LLMError("gemini request failed: 503")
            items = json.loads(prompt[prompt.index("Messages:") + 9:])
            bad = lambda t: bool(self.flag_text and self.flag_text in t)  # noqa: E731
            return {"items": [{"id": i["id"], "compliant": not bad(i["text"]),
                               "issues": ["misleading claim"] if bad(i["text"]) else []} for i in items]}
        if "localiser" in system:
            src = json.loads(prompt[prompt.index("{\n"):])
            return {"_name": "Tamil", **{k: "த " + v for k, v in src.items()}, "report_title": "broken {nope}"}
        assert "Community Guidelines" in system, "guidelines missing from persona prompt"
        if "Analyse these comments" in prompt:
            payload = json.loads(prompt[prompt.index("Comments:") + 9:])
            return {"items": [{"id": c["id"], "category": CANNED[c["id"]][0], "sentiment": CANNED[c["id"]][1],
                               "policy_flag": CANNED[c["id"]][2], "needs_human": False, "reply": CANNED[c["id"]][3]} for c in payload]}
        if "daily briefing" in prompt:
            assert "Write the bullets in" in prompt
            return {"summary": ["Views steady"], "risks": ["1 sponsor email"], "actions": ["Reply to the mic question"]}
        if "upload package" in prompt:
            assert "keyword stuffing" in prompt
            return {"titles": ["SUB4SUB HERE"], "description": "Great video", "tags": ["t" * 60] * 15,
                    "chapters": [], "thumbnail_text": ["X"], "pinned_comment": "What do you think?"}
        raise AssertionError(prompt[:80])

    def text(self, system, prompt, temperature=0.7):
        assert "Community Guidelines" in system
        return "DM me on WhatsApp for guaranteed profit" if "money" in prompt else "It's Python 3.12!"


def make_app(name, mode="auto", **llm_kw):
    app = App(replace(base, db_path=tmp / f"{name}.sqlite3"))
    app.persona["comments"].update(mode=mode, reply_gap_seconds=0)
    app.persona["links"].update(github="https://github.com/me", discord="https://discord.gg/x")
    app._yt, app._llm = FakeYT(), FakeLLM(**llm_kw)
    return app


# ---- .env parser ------------------------------------------------------------
env = tmp / ".env"
env.write_text('A1=plain  # comment\nexport B1="quoted # not comment"\nC1=\n# x=y\n', encoding="utf-8")
load_dotenv(env)
assert os.environ["A1"] == "plain" and os.environ["B1"] == "quoted # not comment" and os.environ["C1"] == ""
ok(".env parser")

# ---- HTTP errors --------------------------------------------------------------
class ErrHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}}).encode()
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), ErrHandler)
threading.Thread(target=srv.handle_request, daemon=True).start()
try:
    request("GET", f"http://127.0.0.1:{srv.server_port}/x")
    raise AssertionError("expected ApiError")
except ApiError as e:
    assert e.status == 403 and e.reason == "quotaExceeded", e
srv.server_close()
try:
    request("GET", "http://127.0.0.1:1/nothing", timeout=2)
except ApiError as e:
    assert e.status == 0
ok("HTTP error parsing")

# ---- policy -------------------------------------------------------------------
P = make_app("p").persona
assert policy.check_text("let's do sub4sub!", P).block
assert policy.check_text("Like to win a free iPhone giveaway", P).block
assert policy.check_text("Code: https://github.com/me/repo", P).ok
assert policy.check_text("visit https://evil.xyz now", P).block
assert policy.check_text("bit.ly/abc", P).block
assert policy.check_text("call +91 98765 43210", P).block
assert policy.check_text("posted on 2026-11-03 at 10:00", P).ok
assert policy.check_text("THIS IS THE BEST VIDEO EVER MADE OMG", P).review
assert policy.check_text("Python 3.12 and node.js are great", P).ok
assert policy.check_text("बहुत बढ़िया सवाल! कोड डिस्क्रिप्शन में है।", P).ok
assert policy.check_text("شكراً جزيلاً على دعمك", P).ok
ok("guideline rule checks (incl. Hindi/Arabic text)")

# ---- comment sweep (auto) ---------------------------------------------------------
app = make_app("auto")
events_before = bus.last_id
st = comments.sweep(app)
posted = dict(app.yt.posted)
rows = {r["id"]: r for r in app.store.pending()}
assert set(posted) == {"c1", "c2", "c14", "c16", "c17"}, posted
assert ("c10", "heldForReview") in app.yt.moderated and ("c4", "heldForReview") in app.yt.moderated
assert all(cid != "c11" for cid, _ in app.yt.moderated)
assert "persona links" in rows["c12"]["note"] and "scam" in rows["c13"]["note"] and "repetitive" in rows["c15"]["note"]
assert "c5" in rows and st["blocked"] == 2
assert any(e["kind"] == "replied" for e in bus.since(events_before))
ok(f"comment sweep, auto mode {st}")

app2 = make_app("down", review_fails=True)
comments.sweep(app2)
assert app2.yt.posted == []
ok("fail-closed when AI review is down")

app3 = make_app("flag", flag_text="Fifine")
comments.sweep(app3)
assert "c1" not in dict(app3.yt.posted)
ok("AI review blocks flagged draft")

app4 = make_app("cap")
app4.persona["comments"]["max_replies_per_day"] = 1
comments.sweep(app4)
assert len(app4.yt.posted) == 1
ok("daily reply cap")

# ---- approve via shared path ----------------------------------------------------------
app5 = make_app("review", mode="review")
comments.sweep(app5)
status, msg = comments.approve_draft(app5, "c1", "Check github.com/me for the code!")
assert status == "posted", (status, msg)
status, msg = comments.approve_draft(app5, "c2", "sub4sub bro")
assert status == "blocked", status
ok("approve_draft guard")

# ---- report + i18n ----------------------------------------------------------------------
txt = report.daily_report(app, send=False)
assert "Test Channel" in txt and "Briefing" in txt
app.persona["ui"]["language"] = "hi"
txt_hi = report.daily_report(app, send=False)
assert "सारांश" in txt_hi and "सब्सक्राइबर" in txt_hi, txt_hi[:300]
assert i18n.strings("ar")["_dir"] == "rtl" and i18n.strings("pt-BR")["_name"] == "Português"
assert i18n.strings("xx")["briefing"] == "Briefing"  # unknown -> English fallback
for code in i18n.available():
    assert set(i18n.strings(code)) >= set(i18n.strings("en")), code
path = i18n.translate_ui(app.llm, "ta", "Tamil")
data = json.loads(path.read_text(encoding="utf-8"))
assert data["_name"] == "Tamil" and "report_title" not in data and data["briefing"].startswith("த ")
path.unlink()
i18n.strings.cache_clear()
app.persona["ui"]["language"] = "en"
ok("report in English + Hindi, RTL, fallback, translate-ui keeps placeholders")

# ---- live chat --------------------------------------------------------------------------
app.persona["live_chat"]["commands"]["!bad"] = "Visit https://scam.xyz for free subs"
bot = livechat.LiveChatBot(app, app.yt, "chat", "Stream")
bot.cfg["min_seconds_between_bot_messages"] = 0
assert "!bad" in bot._disabled_commands
m = lambda t, mid="x", typ="textMessageEvent", **ad: {  # noqa: E731
    "id": mid, "snippet": {"type": typ, "displayMessage": t, **ad.pop("sn", {})},
    "authorDetails": {"displayName": "Ana", "channelId": "UCana", **ad}}
mark = bus.last_id
bot.handle(m("!discord"))
bot.handle(m("!discord"))
bot.handle(m("!bad"))
bot.handle(m("check www.spam.xyz", mid="link"))
bot.handle(m("sub4sub?", mid="s4s"))
bot.handle(m("my site github.com/x", mid="mod", isChatModerator=True))
bot.handle(m("!ask what python?"))
bot.handle(m("!ask how to make money?", channelId="UCbob", displayName="Bob"))
bot.handle(m("", typ="superChatEvent", sn={"superChatDetails": {"amountDisplayString": "₹100", "userComment": "hi"}}))
assert app.yt.sent.count("Join the community: https://discord.gg/x") == 1
assert app.yt.deleted == ["link", "s4s"]
assert any(s.startswith("🤖 @Ana") for s in app.yt.sent) and not any("WhatsApp" in s for s in app.yt.sent)
kinds = [e["kind"] for e in bus.since(mark)]
assert "ask" in kinds and "super_chat" in kinds, kinds
ok("live chat guard, cooldowns, deletions, overlay events")

# ---- content ------------------------------------------------------------------------------
out = content.package(app, "My Topic")
assert "YouTube guideline check" in out and "fake engagement" in out
ok("upload package guideline check")

# ---- scheduler ----------------------------------------------------------------------------
tz = ZoneInfo("Asia/Kolkata")
j = Job("d", lambda: None, daily_at=(9, 0), tz=tz)
assert j.next_run > datetime.now(tz) and (j.next_run.hour, j.next_run.minute) == (9, 0)
j2 = Job("i", lambda: None, every_minutes=30, run_now=True, tz=tz)
assert j2.next_run <= datetime.now(tz)
j2.run()
assert j2.next_run > datetime.now(tz) + timedelta(minutes=29)
ok("scheduler timing")

# ---- web server -----------------------------------------------------------------------------
web = start_web(app)
port = web.server_port
token = web_token(app)
url = f"http://127.0.0.1:{port}"


def get(path, tok=None):
    req = urllib.request.Request(url + path, headers={"X-PixelPaw-Token": tok} if tok else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


assert get("/overlay")[0] == 200 and "EventSource" in get("/overlay")[1]
assert get("/dock")[0] == 200
assert json.loads(get("/api/i18n?lang=ja")[1])["briefing"] == "概要"
assert get("/api/state")[0] == 401 and get("/api/drafts")[0] == 401
code, body = get("/api/state", token)
assert code == 200 and json.loads(body)["channel"]
assert isinstance(json.loads(get("/api/drafts", token)[1]), list)

received = []


def listen():
    with urllib.request.urlopen(url + "/events", timeout=10) as r:
        for line in r:
            if line.startswith(b"data:"):
                received.append(json.loads(line[5:]))
                return


t = threading.Thread(target=listen, daemon=True)
t.start()
import time  # noqa: E402

time.sleep(0.5)
bus.emit("alert", text="private")  # not on the public overlay feed
bus.emit("member", name="Zoe")
t.join(5)
assert received and received[0]["kind"] == "member", received
req = urllib.request.Request(url + "/api/test", data=b"", method="POST", headers={"X-PixelPaw-Token": token})
assert json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"]
web.shutdown()
ok("web server: pages, token protection, i18n API, public SSE feed filters private events")

print(f"\nALL {len(PASSED)} OFFLINE TESTS PASSED")
