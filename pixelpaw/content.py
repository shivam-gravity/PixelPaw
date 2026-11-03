"""Content assistant: video ideas, upload packages (titles/description/tags/chapters), and persona learning."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import yaml

from . import policy
from .intelligence import persona_prompt
from .llm import LLMError
from .notify import save_file


def _fit_tags(tags: list[str], limit: int = 500) -> list[str]:
    """YouTube allows 500 characters of tags in total; keep the first ones that fit."""
    out, used = [], 0
    for t in (str(x).strip().lstrip("#") for x in tags):
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if t and used + cost <= limit:
            out.append(t)
            used += cost
    return out


def _channel_context(app, n: int = 25) -> list[dict]:
    rows = []
    for v in app.yt.recent_uploads(n):
        s = v.get("statistics", {})
        rows.append({"title": v["snippet"]["title"], "views": int(s.get("viewCount", 0) or 0), "published": v["snippet"]["publishedAt"][:10]})
    return rows


def ideas(app, count: int = 10, theme: str = "") -> str:
    videos = _channel_context(app)
    questions = app.store.recent_questions(30)
    prompt = f"""Suggest {count} new video ideas for my channel.

Use what has worked (views below), what viewers keep asking, and my niche.
{f'Focus on this theme: {theme}' if theme else ''}

Return JSON: {{"ideas": [{{"title": "...", "hook": "first 10 seconds", "why": "evidence from the data"}}]}}

My recent videos (title, views, date):
{json.dumps(videos, ensure_ascii=False)}

Recent viewer questions:
{json.dumps(questions, ensure_ascii=False)}"""
    data = app.llm.json(persona_prompt(app.persona), prompt, temperature=0.8)
    lines = [f"# 💡 Video ideas — {datetime.now():%Y-%m-%d}", ""]
    for i, idea in enumerate(data.get("ideas", []), 1):
        lines += [f"## {i}. {idea.get('title', '')}", f"- **Hook:** {idea.get('hook', '')}", f"- **Why:** {idea.get('why', '')}", ""]
    text = "\n".join(lines)
    path = save_file(app.settings.output_dir, f"ideas-{datetime.now():%Y%m%d-%H%M}.md", text)
    return f"{text}\n(saved to {path})"


def package(app, topic: str, transcript_path: str | None = None) -> str:
    transcript = ""
    if transcript_path:
        transcript = Path(transcript_path).read_text(encoding="utf-8", errors="replace")[:30000]
    titles = [v["title"] for v in _channel_context(app, 15)]
    has_ts = bool(re.search(r"\b\d{1,2}:\d{2}\b", transcript))
    transcript_block = "Transcript:\n" + transcript if transcript else "(no transcript provided)"
    chapters_rule = (
        'list of {"time": "MM:SS", "label": "..."} using timestamps from the transcript, first must be 00:00'
        if has_ts
        else "empty list"
    )
    prompt = f"""Prepare the upload package for my next video.

Topic: {topic}
{transcript_block}

My recent titles, for style reference: {json.dumps(titles, ensure_ascii=False)}

Return JSON with:
- "titles": 5 title options (under 70 characters, curiosity + clarity, no clickbait lies)
- "description": the description body, 2-4 short paragraphs, first line must work as a hook in search. No links.
- "tags": 15 tags
- "chapters": {chapters_rule}
- "thumbnail_text": 3 options, max 4 words each
- "pinned_comment": a comment I can pin to start discussion

{policy.METADATA_RULES}"""
    data = app.llm.json(persona_prompt(app.persona), prompt, temperature=0.7)
    data["tags"] = _fit_tags(data.get("tags", []))

    # Guideline check on everything that will be public.
    public = {
        "titles": "\n".join(data.get("titles", [])),
        "description": data.get("description", ""),
        "thumbnail_text": "\n".join(data.get("thumbnail_text", [])),
        "tags": ", ".join(data.get("tags", [])),
        "pinned_comment": data.get("pinned_comment", ""),
    }
    findings: list[str] = []
    for part, text in public.items():
        if text:
            v = policy.check_text(text, app.persona)
            findings += [f"{part}: {x}" for x in v.block + v.review]
    try:
        reviewed = policy.llm_review(app.llm, [
            {"id": k, "text": v, "context": f"YouTube video {k} for a video about: {topic}"} for k, v in public.items() if v
        ])
        findings += [f"{k}: {x}" for k, issues in reviewed.items() for x in issues]
    except LLMError as exc:
        findings.append(f"AI guideline review unavailable ({exc.__class__.__name__}); check manually before publishing")

    links = {k: v for k, v in (app.persona.get("links") or {}).items() if v}
    out = [f"# 🎬 Upload package — {topic}", "", "## Titles"] + [f"{i}. {t}" for i, t in enumerate(data.get("titles", []), 1)]
    out += ["", "## Description", "", data.get("description", "").strip()]
    if data.get("chapters"):
        out += ["", "Chapters:"] + [f"{c.get('time', '')} {c.get('label', '')}" for c in data["chapters"]]
    if links:
        out += [""] + [f"{k.capitalize()}: {v}" for k, v in links.items()]
    out += ["", "## Tags", ", ".join(data.get("tags", []))]
    out += ["", "## Thumbnail text"] + [f"- {t}" for t in data.get("thumbnail_text", [])]
    out += ["", "## Pinned comment", data.get("pinned_comment", "")]
    out += ["", "## YouTube guideline check"]
    out += [f"- ⚠️ {f}" for f in findings] if findings else ["- ✔ No issues found. Still make sure titles and thumbnails match what the video really shows."]
    text = "\n".join(out) + "\n"
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:40] or "video"
    path = save_file(app.settings.output_dir, f"package-{slug}.md", text)
    return f"{text}\n(saved to {path})"


def learn_persona(app) -> Path:
    """Read your channel and your own past replies, and draft a personalised persona file."""
    ch = app.yt.channel(refresh=True)
    my_id = ch["id"]
    titles = [v["title"] for v in _channel_context(app, 30)]
    my_replies: list[str] = []
    for thread in app.yt.comment_threads(max_pages=5):
        for r in (thread.get("replies") or {}).get("comments", []):
            if (r["snippet"].get("authorChannelId") or {}).get("value") == my_id:
                my_replies.append(r["snippet"].get("textOriginal", "")[:300])
        if len(my_replies) >= 40:
            break

    prompt = f"""Study this YouTube channel and describe its creator's persona so a bot can reply to viewers as them.

Channel name: {ch['snippet']['title']}
Handle: {ch['snippet'].get('customUrl', '')}
About: {ch['snippet'].get('description', '')}
Recent video titles: {json.dumps(titles, ensure_ascii=False)}
Creator's own replies to viewers: {json.dumps(my_replies, ensure_ascii=False)}

Return JSON:
{{"niche": "...", "audience": "...", "language": "...", "tone": "...",
  "style_rules": ["5-8 concrete rules, e.g. emoji use, length, slang, how they sign off"],
  "examples": ["up to 10 of the creator's replies that best show their voice, copied verbatim"],
  "faq_topics": ["questions viewers likely ask that the creator should write answers for"]}}"""
    data = app.llm.json(
        "You are an expert at analysing creators' communication style. Be specific, not generic.", prompt, temperature=0.3
    )

    suggested = {
        "channel": {
            "name": ch["snippet"]["title"],
            "handle": ch["snippet"].get("customUrl", ""),
            "niche": data.get("niche", ""),
            "audience": data.get("audience", ""),
            "language": data.get("language", "English"),
        },
        "voice": {
            "tone": data.get("tone", ""),
            "style_rules": data.get("style_rules", []),
            "examples": data.get("examples", []) or my_replies[:10],
        },
        "faq": [{"q": q, "a": "TODO: write your answer"} for q in data.get("faq_topics", [])],
    }
    path = app.settings.persona_file.with_name("persona.suggested.yaml")
    header = (
        "# Generated by `python -m pixelpaw persona-learn`. Review it, fill in the FAQ answers,\n"
        "# then copy the parts you like into persona.yaml.\n"
    )
    path.write_text(header + yaml.safe_dump(suggested, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
    return path
