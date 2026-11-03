"""YouTube Community Guidelines guard.  https://support.google.com/youtube/answer/9288567

Applied to EVERYTHING the bot writes publicly (comment replies, live chat, upload metadata),
and to incoming comments / chat. This module is intentionally not configurable from persona.yaml:
automations and settings cannot switch it off.

Three layers:
  1. GUIDELINES_PROMPT  - every generation prompt carries the rules.
  2. check_text()       - deterministic checks (links, fake engagement, scams, personal info, spammy
                          formatting, repetition). "block" findings can never be posted by the bot.
  3. llm_review()       - an independent compliance review of each message before it is posted.
                          If the review can't run, callers must NOT post (fail closed).
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field

GUIDELINES_URL = "https://support.google.com/youtube/answer/9288567"

POLICY_FLAGS = [
    "none", "scam_spam", "fake_engagement", "harassment", "hate", "sexual", "child_safety",
    "violent_dangerous", "self_harm", "misinformation", "regulated_goods", "impersonation",
]

GUIDELINES_PROMPT = f"""You must follow YouTube's Community Guidelines ({GUIDELINES_URL}) in everything you write:
- Spam & deceptive practices: no repetitive or copy-paste text, no misleading claims, no scams, no "get rich quick" or investment promises, no fake contact details, and no sending people off YouTube except to the creator's own listed links.
- Fake engagement: never offer anything in exchange for likes, subscribers, views or comments; never sub4sub; never mention services that sell views, likes or subscribers. A plain, occasional "subscribe if you enjoyed it" is fine.
- Impersonation: never claim to be another person, creator, brand or YouTube itself; never speak for others.
- Harassment, cyberbullying and hate speech: never insult, mock, threaten, dox or single anyone out, even if they are rude. Never repeat slurs. Stay calm and kind, or say nothing.
- Sensitive content: nothing sexual, nothing inappropriate involving minors, no vulgar language, no graphic violence.
- Harmful or dangerous content: no instructions for or encouragement of dangerous acts, weapons, drugs, or hacking other people. If someone mentions self-harm or suicide, respond only with care and encourage them to contact local emergency services or a crisis line.
- Regulated goods: never offer, sell, or point to illegal or regulated goods or services.
- Misinformation: no medical, election, or other claims that could cause real-world harm. Don't give medical, legal or financial advice; say you're not sure rather than guess.
If a comment or request would require breaking these rules, do not comply: give a short polite deflection, or no reply at all."""

METADATA_RULES = """Metadata rules (YouTube spam & deceptive practices policy):
- Titles, thumbnail text and description must accurately reflect what the video actually contains: no misleading clickbait or promises the video doesn't deliver.
- No keyword stuffing: no long lists of tags or keywords in the description; only relevant tags.
- No incentives for engagement (no "like to win" or "subscribe to enter"), and no links other than the creator's own."""

# ---- deterministic rules --------------------------------------------------
_RULES: list[tuple[str, re.Pattern]] = [
    ("fake engagement (sub4sub / like4like / incentives)", re.compile(
        r"(?i)\b(sub\s*(4|for|2)\s*sub|s4s|l4l|like\s*(4|for)\s*like|subscribe\s+back|follow\s*back|"
        r"(free|buy|cheap|get)\s+(\d+\s*)?(subs|subscribers|views|likes|watch\s*hours)|"
        r"(subscribe|like|comment|share)\b[^.!?\n]{0,40}\b(to\s+(win|enter|get)|for\s+a\s+chance)|"
        r"giveaway|in\s+exchange\s+for\s+(a\s+)?(like|sub|subscription|view))")),
    ("scam / get-rich-quick language", re.compile(
        r"(?i)\b(guaranteed\s+(profit|returns?|income|money)|double\s+your\s+(money|crypto|investment|btc)|"
        r"investment\s+(opportunity|plan|advice)|trading\s+signals?|send\s+(me\s+)?(money|btc|crypto|usdt)|"
        r"claim\s+your\s+(prize|reward)|you('ve|\s+have)\s+won|(dm|text|message|contact)\s+me\s+on\s+"
        r"(whats\s*app|telegram|signal|instagram)|whats\s*app\s*(me|\+?\d))")),
    ("off-platform contact request", re.compile(r"(?i)\b(telegram\s*@\w+|wa\.me/|t\.me/)")),
]
URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)[^\s<>()\"']+|"
    r"\b[a-z0-9][a-z0-9-]*\.(?:com|net|org|io|gg|ly|me|in|co|xyz|app|link|site|shop|store|tk|ru|info|biz|live|tv|to)(?:/[^\s<>()\"']*)?\b"
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\w/])\+?\d[\d\s().-]{8,}\d(?![\w/])")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


@dataclass
class Verdict:
    block: list[str] = field(default_factory=list)   # the bot must never post this
    review: list[str] = field(default_factory=list)  # a human must look first

    @property
    def ok(self) -> bool:
        return not self.block and not self.review

    def summary(self) -> str:
        parts = [f"⛔ {b}" for b in self.block] + [f"⚠️ {r}" for r in self.review]
        return "; ".join(parts)


def _norm_url(url: str) -> str:
    u = url.lower().rstrip(".,!?;:)]}'\"")
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def allowed_links(persona: dict) -> list[str]:
    return [_norm_url(v) for v in (persona.get("links") or {}).values() if isinstance(v, str) and v.strip()]


def _canon(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"@\S+", "", text.lower())).strip()


def check_text(text: str, persona: dict, recent: list[str] | tuple = ()) -> Verdict:
    """Deterministic guideline checks for text the bot is about to post."""
    v = Verdict()
    t = (text or "").strip()
    if not t:
        v.block.append("empty message")
        return v

    allowed = allowed_links(persona)
    allowed_raw = " ".join((persona.get("links") or {}).values()).lower() if persona.get("links") else ""
    for email in EMAIL_RE.findall(t):
        if email.lower() not in allowed_raw:
            v.block.append(f"email address not in your links: {email}")
    scan = EMAIL_RE.sub(" ", t)
    for url in URL_RE.findall(scan):
        n = _norm_url(url)
        if not any(n == a or n.startswith(a + "/") or n.startswith(a + "?") for a in allowed):
            v.block.append(f"link not in your persona links: {url}")
    for phone in PHONE_RE.findall(scan):
        if sum(ch.isdigit() for ch in phone) >= 10:
            v.block.append("phone number (personal info / fake-contact risk)")
            break
    for name, rx in _RULES:
        if rx.search(t):
            v.block.append(name)

    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 20 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
        v.review.append("mostly capital letters (spammy)")
    if re.search(r"(.)\1{5,}", t):
        v.review.append("long repeated characters (spammy)")
    if len(EMOJI_RE.findall(t)) > 5:
        v.review.append("too many emoji (spammy)")
    canon = _canon(t)
    if canon and len(canon) > 25:
        for prev in recent:
            if difflib.SequenceMatcher(None, canon, _canon(prev)).ratio() >= 0.9:
                v.review.append("near-identical to a recent bot message (repetitive content)")
                break
    return v


def incoming_violation(text: str) -> str | None:
    """Cheap check on viewer chat/comment text: returns a reason if it's obvious spam/scam."""
    for name, rx in _RULES:
        if rx.search(text or ""):
            return name
    return None


def has_link(text: str) -> bool:
    return bool(URL_RE.search(EMAIL_RE.sub(" ", text or "")))


# ---- AI compliance review -------------------------------------------------
def llm_review(llm, items: list[dict]) -> dict[str, list[str]]:
    """items: [{"id", "text", "context"}]. Returns {id: issues}; [] means compliant.

    Raises LLMError if the review cannot be done -- callers must then NOT post.
    """
    if not items:
        return {}
    payload = [{"id": str(i["id"]), "context": (i.get("context") or "")[:600], "text": i["text"]} for i in items]
    prompt = f"""Check each message a YouTube channel is about to post publicly against YouTube's Community Guidelines ({GUIDELINES_URL}):
spam & deceptive practices (misleading claims, scams, repetitive text, off-platform diversion, misleading metadata/clickbait),
fake engagement (incentives for likes/subs/views, sub4sub), impersonation, harassment & cyberbullying, hate speech,
sexual content, child safety, vulgar language, violent or dangerous content, self-harm, regulated goods,
harmful misinformation (medical, elections), and sharing personal information.

"context" is what the message responds to. Friendly, ordinary replies are compliant: flag only genuine problems.
Return JSON: {{"items": [{{"id": "...", "compliant": true or false, "issues": ["short reason", ...]}}]}}

Messages:
{json.dumps(payload, ensure_ascii=False, indent=1)}"""
    data = llm.json("You are a precise YouTube Trust & Safety policy reviewer.", prompt, temperature=0)
    out: dict[str, list[str]] = {}
    for item in data.get("items", []):
        iid = str(item.get("id", ""))
        if item.get("compliant") is True:
            out[iid] = []
        else:
            out[iid] = [str(x) for x in item.get("issues") or []] or ["flagged by guideline review"]
    for i in payload:
        out.setdefault(i["id"], ["guideline review returned no verdict"])
    return out


def full_check(llm, persona: dict, text: str, context: str = "", recent=()) -> Verdict:
    """Deterministic + AI review for a single message. AI failure becomes a 'block' (fail closed)."""
    from .llm import LLMError

    v = check_text(text, persona, recent)
    if v.block:
        return v
    try:
        issues = llm_review(llm, [{"id": "1", "text": text, "context": context}]).get("1", [])
    except LLMError as exc:
        v.block.append(f"guideline review unavailable ({exc.__class__.__name__}); not posting")
        return v
    v.block += [f"guideline review: {i}" for i in issues]
    return v
