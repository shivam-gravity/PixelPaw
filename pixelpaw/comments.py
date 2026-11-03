"""Comment routine: collect new comments -> analyse -> automations -> guideline guard -> reply or draft."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from .net import ApiError

from . import automations, intelligence, notify, policy
from .events import bus
from .llm import LLMError
from .youtube import error_reason

log = logging.getLogger(__name__)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def collect_new_comments(app) -> list[dict]:
    """New top-level comments the bot hasn't handled yet. Skipped ones are recorded so they aren't refetched."""
    cfg = app.persona["comments"]
    my_id = app.yt.channel_id
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["lookback_hours"])
    skip_authors = {a.lower().lstrip("@") for a in cfg.get("skip_authors") or []}
    fresh: list[dict] = []

    for thread in app.yt.comment_threads(max_pages=cfg["max_pages"]):
        top = thread["snippet"]["topLevelComment"]
        s = top["snippet"]
        if _parse_ts(s["publishedAt"]) < cutoff:
            break  # threads are newest-first
        if app.store.is_known(top["id"]):
            continue
        author_id = (s.get("authorChannelId") or {}).get("value", "")
        rec = {
            "id": top["id"],
            "video_id": thread["snippet"].get("videoId") or s.get("videoId", ""),
            "author": s.get("authorDisplayName", ""),
            "author_id": author_id,
            "text": s.get("textOriginal") or s.get("textDisplay", ""),
            "published_at": s["publishedAt"],
            "can_reply": thread["snippet"].get("canReply", True),
        }
        if author_id == my_id:
            app.store.upsert_comment({**rec, "status": "own"})
            continue
        replies = (thread.get("replies") or {}).get("comments", [])
        already = any((r["snippet"].get("authorChannelId") or {}).get("value") == my_id for r in replies)
        too_short = len(rec["text"].strip()) < cfg["min_comment_length"]
        skipped_author = rec["author"].lower().lstrip("@") in skip_authors
        if already or too_short or skipped_author or not rec["can_reply"]:
            note = "already replied" if already else "too short" if too_short else "skipped author" if skipped_author else "replies disabled"
            app.store.upsert_comment({**rec, "status": "skipped", "note": note})
            continue
        fresh.append(rec)
    return fresh


def _apply_policy_flag(c: dict, a: dict, d: automations.Decision) -> None:
    """Guideline-violating comments are never replied to. Not overridable by automations."""
    flag = a.get("policy_flag", "none")
    if flag == "none":
        return
    d.reply = False
    if flag == "self_harm":
        d.moderate = None  # don't silently hide someone who may need help; the creator decides
        d.notify.append("⚠️ Possible self-harm comment, please look at it personally")
    else:
        d.moderate = "heldForReview"
        d.notify.append(f"Guideline violation in comment ({flag}), held for review")


def guard_drafts(app, candidates: list[dict]) -> dict[str, policy.Verdict]:
    """Run the guideline guard on reply drafts. AI review failure blocks everything (fail closed)."""
    recent = app.store.recent_reply_texts(200)
    verdicts = {c["id"]: policy.check_text(c["draft"], app.persona, recent) for c in candidates}
    to_review = [
        {"id": c["id"], "text": c["draft"], "context": f"Viewer comment on \"{c.get('video_title', '')}\": {c['text']}"}
        for c in candidates if not verdicts[c["id"]].block
    ]
    if to_review:
        try:
            issues = policy.llm_review(app.llm, to_review)
        except LLMError as exc:
            log.error("Guideline review failed, nothing will be posted this run: %s", exc)
            issues = {i["id"]: ["guideline review unavailable, try again later"] for i in to_review}
        for cid, found in issues.items():
            if cid in verdicts:
                verdicts[cid].block += [f"guideline review: {x}" for x in found]
    # Drafts within one batch must not be near-duplicates of each other either.
    seen: list[str] = []
    for c in candidates:
        v = policy.check_text(c["draft"], app.persona, seen)
        verdicts[c["id"]].review += [r for r in v.review if "repetitive" in r and r not in verdicts[c["id"]].review]
        seen.append(c["draft"])
    return verdicts


def sweep(app, dry_run: bool = False) -> dict:
    """One pass of the comment routine. Returns counts for logging/reporting."""
    cfg = app.persona["comments"]
    stats = {"new": 0, "replied": 0, "drafted": 0, "moderated": 0, "blocked": 0, "alerts": 0, "failed": 0}
    fresh = collect_new_comments(app)
    stats["new"] = len(fresh)
    if not fresh:
        log.info("No new comments.")
        return stats

    titles = {vid: v["snippet"]["title"] for vid, v in app.yt.videos([c["video_id"] for c in fresh]).items()}
    for c in fresh:
        c["video_title"] = titles.get(c["video_id"], "")

    analysis = intelligence.analyse_comments(app.llm, app.persona, fresh)
    alerts: list[str] = []
    plans = []  # (comment, analysis, decision)
    for c in fresh:
        a = analysis.get(c["id"])
        if a is None:  # LLM batch failed: leave unrecorded so the next sweep retries it
            continue
        d = automations.evaluate(app.persona, c, a)
        _apply_policy_flag(c, a, d)
        c["draft"] = a["reply"] if d.reply else ""
        plans.append((c, a, d))
        if d.notify:
            alerts.append(f"{', '.join(d.notify)}: {c['author']} on \"{c['video_title']}\": {c['text'][:300]}")

    candidates = [c for c, _, _ in plans if c["draft"]]
    verdicts = guard_drafts(app, candidates) if candidates else {}

    auto = cfg["mode"] == "auto"
    day_start = _utc_iso(datetime.now(timezone.utc) - timedelta(hours=24))
    replied_today = app.store.replies_since(day_start)

    for c, a, d in plans:
        v = verdicts.get(c["id"], policy.Verdict())
        rec = {**{k: c.get(k) for k in ("id", "video_id", "video_title", "author", "author_id", "text", "published_at", "draft")},
               "category": a["category"], "sentiment": a["sentiment"], "status": "seen"}

        if dry_run:
            action = ("moderate:" + d.moderate) if d.moderate else ("reply" if c["draft"] else "none")
            print(f"[{a['category']}/{a['sentiment']}/policy:{a.get('policy_flag')}] {c['author']}: {c['text'][:120]}\n"
                  f"   -> {action}: {c['draft']}" + (f"\n   guard: {v.summary()}" if not v.ok else "") + "\n")
            continue

        if d.moderate:
            try:
                app.yt.set_moderation(c["id"], d.moderate)
                rec.update(status="moderated", note=d.moderate)
                stats["moderated"] += 1
            except ApiError as exc:
                rec["note"] = f"moderation failed: {error_reason(exc) or exc}"
        elif c["draft"]:
            if v.block:
                rec.update(status="pending", note="draft blocked by guideline guard: " + v.summary())
                stats["blocked"] += 1
            elif (auto and v.ok and not d.force_review
                  and stats["replied"] < cfg["max_replies_per_run"]
                  and replied_today < cfg["max_replies_per_day"]):
                try:
                    if stats["replied"]:
                        time.sleep(cfg["reply_gap_seconds"])  # no bursts of bot-like activity
                    rec["reply_id"] = app.yt.reply(c["id"], c["draft"])
                    rec["status"] = "replied"
                    bus.emit("replied", author=c["author"], text=c["text"][:200], reply=c["draft"])
                    stats["replied"] += 1
                    replied_today += 1
                except ApiError as exc:
                    reason = error_reason(exc)
                    rec.update(status="pending", note=f"auto-reply failed: {reason or exc}")
                    stats["failed"] += 1
                    if reason == "quotaExceeded":
                        auto = False
            else:
                rec["status"] = "pending"
                if not v.ok:
                    rec["note"] = v.summary()
                elif auto and d.force_review:
                    rec["note"] = "needs your eyes (negative, sensitive, or an automation asked for review)"
                stats["drafted"] += 1
        app.store.upsert_comment(rec)

    if alerts and not dry_run:
        stats["alerts"] = len(alerts)
        notify.alert(app.settings, f"{len(alerts)} comment alert(s):\n" + "\n".join(f"• {x}" for x in alerts))
    log.info("Comment sweep: %s", stats)
    return stats


def approve_draft(app, comment_id: str, text: str | None = None, allow_warnings: bool = False) -> tuple[str, str]:
    """Post a pending draft (optionally edited) after the guideline guard.

    Returns (status, message): status is "posted", "blocked", "warn" (needs allow_warnings) or "error".
    """
    row = next((r for r in app.store.pending() if r["id"] == comment_id), None)
    if row is None:
        return "error", "draft not found (already handled?)"
    text = (text if text is not None else row["draft"] or "").strip()
    if not text:
        return "error", "empty reply"
    context = f"Viewer comment on \"{row['video_title']}\": {row['text']}"
    v = policy.full_check(app.llm, app.persona, text, context, app.store.recent_reply_texts(200))
    if v.block:
        return "blocked", v.summary()
    if v.review and not allow_warnings:
        return "warn", v.summary()
    try:
        reply_id = app.yt.reply(row["id"], text)
    except ApiError as exc:
        return "error", error_reason(exc) or str(exc)
    app.store.set_status(row["id"], "replied", reply_id=reply_id, draft=text)
    bus.emit("replied", author=row["author"], text=row["text"][:200], reply=text)
    return "posted", text


def discard_draft(app, comment_id: str) -> None:
    app.store.set_status(comment_id, "skipped", note="discarded in review")


def review(app) -> None:
    """Interactive approval of pending reply drafts. Every reply still passes the guideline guard."""
    pending = app.store.pending()
    if not pending:
        print("No drafts waiting for review.")
        return
    print(f"{len(pending)} draft(s) to review.  [a]pprove  [e]dit  [s]kip  [d]iscard  [q]uit\n")
    for i, row in enumerate(pending, 1):
        print("─" * 70)
        print(f"{i}/{len(pending)}  {row['video_title'] or row['video_id']}")
        print(f"{row['author']} ({row['category']}, {row['sentiment']}): {row['text']}")
        if row.get("note"):
            print(f"note: {row['note']}")
        print(f"\nDraft: {row['draft'] or '(none)'}\n")
        while True:
            choice = input("> ").strip().lower()[:1]
            if choice in ("a", "e"):
                text = row["draft"] or ""
                if choice == "e" or not text:
                    text = input("Your reply: ").strip()
                    if not text:
                        continue
                status, msg = approve_draft(app, row["id"], text)
                if status == "warn" and input(f"⚠️ {msg}\n  Post anyway? [y/N] ").strip().lower() == "y":
                    status, msg = approve_draft(app, row["id"], text, allow_warnings=True)
                if status == "posted":
                    print("✔ posted")
                    break
                if status == "blocked":
                    print(f"✖ Not posted: breaks YouTube guidelines or can't be verified:\n   {msg}\n  Edit it ([e]) or skip.")
                elif status == "error":
                    print(f"✖ failed: {msg}")
                    break
                continue
            if choice == "s":
                break
            if choice == "d":
                discard_draft(app, row["id"])
                break
            if choice == "q":
                return
            print("a / e / s / d / q")
