"""Daily channel report (modelled on DailyBot's check-in report):
collect -> Intelligence summary on top -> flagged items -> deliver to your channels.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .net import ApiError

from . import comments, intelligence, notify
from .i18n import strings, t
from .llm import LLMError

log = logging.getLogger(__name__)


def _n(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _delta(now: int, before: int | None) -> str:
    if before is None:
        return ""
    d = now - before
    return f" ({'+' if d >= 0 else ''}{d:,})"


def gather(app) -> dict:
    tz = ZoneInfo(app.settings.timezone)
    today = datetime.now(tz).date()
    ch = app.yt.channel(refresh=True)
    st = ch["statistics"]
    subs, views, videos = _n(st.get("subscriberCount")), _n(st.get("viewCount")), _n(st.get("videoCount"))
    prev = app.store.last_snapshot_before(today.isoformat())
    app.store.save_snapshot(today.isoformat(), subs, views, videos)

    uploads = []
    for v in app.yt.recent_uploads(app.persona["report"]["recent_uploads"]):
        s = v.get("statistics", {})
        uploads.append({
            "title": v["snippet"]["title"],
            "published": v["snippet"]["publishedAt"][:10],
            "views": _n(s.get("viewCount")),
            "likes": _n(s.get("likeCount")),
            "comments": _n(s.get("commentCount")),
            "url": f"https://youtu.be/{v['id']}",
        })

    analytics = []
    try:  # Analytics data lags 1-3 days; show what's available for the last week.
        analytics = app.yt.daily_analytics(today - timedelta(days=8), today - timedelta(days=1))
    except ApiError as exc:
        log.warning("Analytics unavailable: %s", exc)

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    recent = app.store.comments_since(since.replace("+00:00", "Z"))
    by_cat = Counter(c["category"] for c in recent if c["category"])
    open_questions = [c for c in recent if c["category"] == "question" and c["status"] != "replied"]
    flagged = [c for c in recent if c["category"] in ("collab", "criticism") or c["sentiment"] == "negative"]

    return {
        "date": today.isoformat(),
        "channel": ch["snippet"]["title"],
        "subscribers": subs, "views": views, "videos": videos,
        "prev": prev,
        "hidden_subs": bool(st.get("hiddenSubscriberCount")),
        "uploads": uploads,
        "analytics": analytics,
        "comments_24h": len(recent),
        "comments_by_category": dict(by_cat),
        "replied_24h": sum(1 for c in recent if c["status"] == "replied"),
        "pending_drafts": len(app.store.pending()),
        "open_questions": [{"author": c["author"], "text": c["text"][:200], "video": c["video_title"]} for c in open_questions[:8]],
        "flagged": [{"author": c["author"], "text": c["text"][:200], "video": c["video_title"], "category": c["category"]} for c in flagged[:8]],
    }


def render(facts: dict, brief: dict | None, lang: str = "en") -> str:
    T = lambda key, **kw: t(lang, key, **kw)  # noqa: E731
    prev = facts["prev"] or {}
    has_prev = bool(prev)
    L = [f"# 📊 {T('report_title', channel=facts['channel'], date=facts['date'])}", ""]

    if brief:
        L += [f"## 🧠 {T('briefing')}"] + [f"- {x}" for x in brief["summary"]]
        if brief["risks"]:
            L += ["", f"**⚠️ {T('needs_attention')}**"] + [f"- {x}" for x in brief["risks"]]
        if brief["actions"]:
            L += ["", f"**✅ {T('do_today')}**"] + [f"- {x}" for x in brief["actions"]]
        L.append("")

    subs = T("hidden") if facts["hidden_subs"] else f"{facts['subscribers']:,}{_delta(facts['subscribers'], prev.get('subscribers') if has_prev else None)}"
    L += [
        f"## {T('channel')}",
        f"- {T('subscribers')}: {subs}",
        f"- {T('total_views')}: {facts['views']:,}{_delta(facts['views'], prev.get('views') if has_prev else None)}",
        f"- {T('videos')}: {facts['videos']:,}",
    ]
    if has_prev:
        L.append(f"- ({T('change_since', day=prev['day'])})")
    L.append("")

    if facts["analytics"]:
        L += [f"## {T('last_days')}", "```", f"{'day':<11}{'views':>8}{'watch min':>11}{'+subs':>7}{'-subs':>7}"]
        for r in facts["analytics"]:
            L.append(f"{r['day']:<11}{_n(r.get('views')):>8,}{_n(r.get('estimatedMinutesWatched')):>11,}"
                     f"{_n(r.get('subscribersGained')):>7}{_n(r.get('subscribersLost')):>7}")
        L += ["```", ""]

    if facts["uploads"]:
        L.append(f"## {T('recent_uploads')}")
        for u in facts["uploads"]:
            L.append(f"- [{u['title']}]({u['url']}) · {u['published']} · {u['views']:,} {T('views')} · "
                     f"{u['likes']:,} {T('likes')} · {u['comments']:,} {T('comments')}")
        L.append("")

    L += [
        f"## {T('comments_24h')}",
        f"- {T('new')}: {facts['comments_24h']} · {T('replied')}: {facts['replied_24h']} · {T('drafts_waiting')}: {facts['pending_drafts']}",
    ]
    if facts["comments_by_category"]:
        L.append(f"- {T('by_type')}: " + ", ".join(f"{k} {v}" for k, v in sorted(facts["comments_by_category"].items(), key=lambda kv: -kv[1])))
    if facts["open_questions"]:
        L += ["", f"**{T('unanswered')}**"] + [f"- {q['author']} ({q['video']}): {q['text']}" for q in facts["open_questions"]]
    if facts["flagged"]:
        L += ["", f"**{T('flagged')}**"] + [f"- [{f['category']}] {f['author']} ({f['video']}): {f['text']}" for f in facts["flagged"]]
    if facts["pending_drafts"]:
        L += ["", f"_{T('run_review')}_"]
    return "\n".join(L) + "\n"


def daily_report(app, send: bool = True) -> str:
    if app.persona["report"].get("sweep_comments_first", True):
        try:
            comments.sweep(app)
        except Exception as exc:  # report must still go out
            log.error("Comment sweep before report failed: %s", exc)

    lang = app.persona["ui"]["language"]
    facts = gather(app)
    brief = None
    try:
        brief = intelligence.summarise_report(app.llm, app.persona, facts, strings(lang).get("_name", lang))
    except LLMError as exc:
        log.error("AI briefing failed, sending report without it: %s", exc)

    text = render(facts, brief, lang)
    targets = app.persona["report"]["deliver_to"] if send else ["file"]
    done = notify.deliver(app.settings, targets, f"{facts['date']}.md", text)
    log.info("Report delivered to: %s", ", ".join(done) or "nowhere")
    return text
