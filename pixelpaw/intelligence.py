"""The "Intelligence" layer (modelled on DailyBot Intelligence):
classify each response, flag what needs attention, and write summaries -- in your voice.
"""
from __future__ import annotations

import json
import logging

from .llm import LLM, LLMError
from .policy import GUIDELINES_PROMPT, POLICY_FLAGS

log = logging.getLogger(__name__)

CATEGORIES = ["question", "praise", "feedback", "criticism", "collab", "spam", "other"]
SENTIMENTS = ["positive", "neutral", "negative"]


def persona_prompt(persona: dict) -> str:
    ch, voice = persona["channel"], persona["voice"]
    lines = [
        f"You are the creator of the YouTube channel \"{ch['name']}\""
        + (f" ({ch['handle']})" if ch.get("handle") else "")
        + ". Always write in first person, as the creator yourself, never as an assistant or bot.",
    ]
    if ch.get("niche"):
        lines.append(f"The channel is about: {ch['niche']}.")
    if ch.get("audience"):
        lines.append(f"The audience is: {ch['audience']}.")
    lines.append(f"Tone of voice: {voice['tone']}.")
    if voice.get("style_rules"):
        lines.append("Style rules:\n" + "\n".join(f"- {r}" for r in voice["style_rules"]))
    if voice.get("never"):
        lines.append("Never:\n" + "\n".join(f"- {r}" for r in voice["never"]))
    if voice.get("examples"):
        lines.append(
            "Real examples of how I reply to viewers (match this voice):\n"
            + "\n".join(f"- {e}" for e in voice["examples"][:15])
        )
    if persona.get("faq"):
        lines.append(
            "Facts you may use when answering (do not invent other facts):\n"
            + "\n".join(f"Q: {f.get('q', '')}\nA: {f.get('a', '')}" for f in persona["faq"])
        )
    links = {k: v for k, v in (persona.get("links") or {}).items() if v}
    if links:
        lines.append("My links (only share these exact URLs):\n" + "\n".join(f"- {k}: {v}" for k, v in links.items()))
    if voice.get("reply_in_commenter_language", True):
        lines.append(f"Reply in the same language the viewer wrote in (default {ch.get('language', 'English')}).")
    else:
        lines.append(f"Always reply in {ch.get('language', 'English')}.")
    lines.append(GUIDELINES_PROMPT)  # always last, so nothing above can override it
    return "\n\n".join(lines)


def analyse_comments(llm: LLM, persona: dict, comments: list[dict], batch_size: int = 15) -> dict[str, dict]:
    """Return {comment_id: {category, sentiment, needs_human, reply}} for each comment."""
    reply_to = persona["comments"]["reply_to"]
    max_chars = persona["voice"]["max_reply_chars"]
    system = persona_prompt(persona)
    results: dict[str, dict] = {}

    for start in range(0, len(comments), batch_size):
        batch = comments[start : start + batch_size]
        payload = [
            {"id": c["id"], "video_title": c.get("video_title", ""), "author": c.get("author", ""), "text": c["text"][:1500]}
            for c in batch
        ]
        prompt = f"""Analyse these comments viewers left on my YouTube videos.

For each comment return an object with:
- "id": copied exactly
- "category": one of {CATEGORIES}
    collab = business, sponsorship or collaboration inquiry
    spam = scams, bots, unrelated self-promotion, suspicious links, impersonation
- "sentiment": one of {SENTIMENTS}
- "policy_flag": the YouTube Community Guidelines issue in the COMMENT itself, one of {POLICY_FLAGS}
    ("none" for normal comments, including polite criticism)
- "needs_human": true if answering properly needs information you do not have, or the comment is sensitive
- "reply": my reply, ONLY when category is one of {reply_to}, needs_human is false and policy_flag is "none"; otherwise "".
    Keep it under {max_chars} characters, natural and specific to what they said, no hashtags,
    do not start with "Thanks for your comment", do not include links unless they are in my links list.

Respond with JSON: {{"items": [ ... ]}}

Comments:
{json.dumps(payload, ensure_ascii=False, indent=1)}"""
        try:
            data = llm.json(system, prompt)
        except LLMError as exc:
            log.error("Comment analysis failed for a batch of %d: %s", len(batch), exc)
            continue
        for item in data.get("items", []):
            cid = item.get("id")
            if not cid:
                continue
            category = item.get("category") if item.get("category") in CATEGORIES else "other"
            sentiment = item.get("sentiment") if item.get("sentiment") in SENTIMENTS else "neutral"
            flag = item.get("policy_flag") if item.get("policy_flag") in POLICY_FLAGS else "none"
            results[cid] = {
                "category": category,
                "sentiment": sentiment,
                "policy_flag": flag,
                "needs_human": bool(item.get("needs_human")),
                "reply": finalise_reply(item.get("reply") or "", persona) if flag == "none" else "",
            }
    return results


def finalise_reply(text: str, persona: dict) -> str:
    text = text.strip().strip('"').strip()
    if not text:
        return ""
    max_chars = persona["voice"]["max_reply_chars"]
    sig = (persona["voice"].get("signature") or "").strip()
    room = max_chars - (len(sig) + 1 if sig else 0)
    if len(text) > room:
        cut = text[:room].rsplit(" ", 1)[0].rstrip(",;:")
        text = cut + "…"
    return f"{text} {sig}" if sig else text


def summarise_report(llm: LLM, persona: dict, facts: dict, language: str = "English") -> dict:
    """Return {"summary": [...], "risks": [...], "actions": [...]} from the day's facts."""
    prompt = f"""Here is today's data for my YouTube channel. Write my daily briefing.

Return JSON with:
- "summary": 3-5 short bullets: what happened, with numbers
- "risks": 0-3 bullets: anything that needs attention (drops, unanswered questions, negative feedback, spam waves, business emails to check)
- "actions": 1-3 concrete things I should do today

Write the bullets in {language}. Be direct, no fluff, don't repeat the raw tables. Data:
{json.dumps(facts, ensure_ascii=False, indent=1, default=str)}"""
    data = llm.json(persona_prompt(persona), prompt, temperature=0.3)
    return {k: [str(x) for x in data.get(k, []) if x] for k in ("summary", "risks", "actions")}
