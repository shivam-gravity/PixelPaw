"""The "Automations" layer (modelled on DailyBot Automations): rules that act on response content.

Each rule in persona.yaml looks like:

    - name: Business inquiries
      when: {category: collab}                 # category / sentiment / contains_any / min_length
      then: {notify: true, reply: false}       # notify / reply / moderate: heldForReview | rejected

All conditions in `when` must match. Later rules override earlier ones for `reply` and `moderate`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MODERATION_STATES = ("heldForReview", "rejected")


@dataclass
class Decision:
    reply: bool
    notify: list[str] = field(default_factory=list)
    moderate: str | None = None
    force_review: bool = False


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def matches(when: dict, comment: dict, analysis: dict) -> bool:
    text = (comment.get("text") or "").lower()
    if "category" in when and analysis.get("category") not in _as_list(when["category"]):
        return False
    if "sentiment" in when and analysis.get("sentiment") not in _as_list(when["sentiment"]):
        return False
    if "contains_any" in when and not any(str(w).lower() in text for w in _as_list(when["contains_any"])):
        return False
    if "min_length" in when and len(text) < int(when["min_length"]):
        return False
    return True


def evaluate(persona: dict, comment: dict, analysis: dict) -> Decision:
    cfg = persona["comments"]
    decision = Decision(reply=analysis.get("category") in cfg["reply_to"])
    if analysis.get("needs_human"):
        decision.force_review = True
    if cfg.get("review_negative", True) and analysis.get("sentiment") == "negative":
        decision.force_review = True

    for rule in persona.get("automations") or []:
        if not matches(rule.get("when") or {}, comment, analysis):
            continue
        then = rule.get("then") or {}
        if "reply" in then:
            decision.reply = bool(then["reply"])
        if then.get("notify"):
            decision.notify.append(rule.get("name", "automation"))
        if then.get("moderate") in MODERATION_STATES:
            decision.moderate = then["moderate"]
        if then.get("review"):
            decision.force_review = True
    if decision.moderate:
        decision.reply = False
    return decision
