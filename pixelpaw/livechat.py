"""Live chat bot: !commands, !ask (answers in your voice), Super Chat thanks, spam/link moderation.

Cost control for !ask: your FAQ answers first (free), then a cache of answers that already passed the
guideline review, then the AI. Per-viewer and per-minute limits stop anyone from draining the budget.
Reliability: polling backs off on errors, slows down when YouTube quota runs low, and the watcher
restarts the bot if it ever crashes.
"""
from __future__ import annotations

import difflib
import hashlib
import logging
import re
import threading
import time
from collections import Counter, defaultdict, deque

from . import policy
from .events import bus, live_state
from .health import health
from .intelligence import persona_prompt
from .llm import LLMError
from .net import ApiError
from .youtube import error_reason

log = logging.getLogger(__name__)

STOP_REASONS = {"liveChatEnded", "liveChatNotFound", "liveChatDisabled", "forbidden", "quotaExceeded"}
REPEAT_WINDOW_S = 180  # identical bot answers within 3 minutes count as repetitive chat


class _Blank(dict):
    def __missing__(self, key):
        return ""


def normalise_question(text: str) -> str:
    text = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return " ".join(text.split())


class LiveChatBot:
    def __init__(self, app, yt, chat_id: str, title: str = ""):
        self.app, self.yt, self.chat_id, self.title = app, yt, chat_id, title
        self.cfg = app.persona["live_chat"]
        self.my_id = yt.channel_id
        self.chatters: Counter = Counter()
        self._last_ask: dict[str, float] = {}
        self._asks_by_user: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
        self._asks_all: deque[float] = deque(maxlen=200)
        self._last_cmd: dict[str, float] = {}
        self._last_sent = 0.0
        self._recent_ai: deque[tuple[float, str]] = deque(maxlen=40)
        links = {k: v for k, v in (app.persona.get("links") or {}).items() if v}
        self._vars = _Blank(links, channel=app.persona["channel"]["name"], title=title)
        self._disabled_commands = self._validate_templates()

    def _validate_templates(self) -> set[str]:
        """Check the fixed messages in persona settings against the guidelines once, at startup."""
        disabled = set()
        named = {"greeting": self.cfg.get("greeting") or "",
                 "super_chat_thanks": self.cfg.get("super_chat_thanks") or "",
                 "new_member_thanks": self.cfg.get("new_member_thanks") or ""}
        named.update({k.lower(): v for k, v in (self.cfg.get("commands") or {}).items()})
        for key, template in named.items():
            if not template:
                continue
            v = policy.check_text(self.fill(template, name="viewer", amount="₹100"), self.app.persona)
            if v.block:
                log.error("Live chat message '%s' disabled, breaks YouTube guidelines: %s", key, v.summary())
                disabled.add(key)
        return disabled

    # ---- output ---------------------------------------------------------
    def say(self, text: str, recent=()) -> bool:
        """Send a chat message. Anything the guideline guard flags is dropped (no human is there to review)."""
        text = text.strip()
        if not text:
            return False
        v = policy.check_text(text, self.app.persona, recent)
        if not v.ok:
            log.warning("Not sending chat message (%s): %s", v.summary(), text[:120])
            return False
        gap = self.cfg["min_seconds_between_bot_messages"] - (time.monotonic() - self._last_sent)
        if gap > 0:
            time.sleep(gap)
        try:
            self.yt.live_send(self.chat_id, text[:200])
            self._last_sent = time.monotonic()
            log.info("bot » %s", text[:200])
            return True
        except ApiError as exc:
            log.error("Could not send chat message: %s", error_reason(exc) or exc)
            return False

    def _delete(self, msg: dict, name: str, why: str) -> None:
        if self.cfg["moderation_action"] != "delete":
            return
        try:
            self.yt.live_delete(msg["id"])
            log.info("Deleted message from %s (%s)", name, why)
        except ApiError as exc:
            log.error("Delete failed: %s", error_reason(exc) or exc)

    def fill(self, template: str, **extra) -> str:
        try:
            return template.format_map(_Blank(self._vars, **extra))
        except (ValueError, IndexError):
            return template

    # ---- input ----------------------------------------------------------
    def handle(self, msg: dict) -> None:
        sn, author = msg.get("snippet", {}), msg.get("authorDetails", {})
        if author.get("channelId") == self.my_id:
            return
        name = author.get("displayName", "someone")
        kind = sn.get("type")

        if kind == "superChatEvent":
            details = sn.get("superChatDetails") or {}
            amount = details.get("amountDisplayString", "")
            bus.emit("super_chat", name=name, amount=amount, message=details.get("userComment", ""))
            if self.cfg["thank_super_chats"] and "super_chat_thanks" not in self._disabled_commands:
                self.say(self.fill(self.cfg["super_chat_thanks"], name=name, amount=amount))
            return
        if kind == "newSponsorEvent":
            bus.emit("member", name=name)
            if self.cfg.get("new_member_thanks") and "new_member_thanks" not in self._disabled_commands:
                self.say(self.fill(self.cfg["new_member_thanks"], name=name))
            return
        if kind != "textMessageEvent":
            return

        text = (sn.get("displayMessage") or "").strip()
        self.chatters[name] += 1
        live_state.chatters = len(self.chatters)
        is_staff = author.get("isChatOwner") or author.get("isChatModerator")

        if not is_staff:
            banned = [w.lower() for w in self.cfg.get("banned_words") or []]
            if banned and any(w in text.lower() for w in banned):
                self._delete(msg, name, "banned word")
                return
            spam = policy.incoming_violation(text)
            if spam:
                self._delete(msg, name, spam)
                return
            if self.cfg.get("delete_viewer_links", True) and policy.has_link(text):
                self._delete(msg, name, "link from viewer")
                return

        if not text.startswith("!"):
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        commands = {k.lower(): v for k, v in (self.cfg.get("commands") or {}).items()
                    if v and k.lower() not in self._disabled_commands}

        if cmd == self.cfg["ask_command"].lower():
            self.ask(name, author.get("channelId", name), arg.strip())
            return
        if cmd not in commands and cmd not in ("!commands", "!top"):
            return
        # The same canned answer over and over is repetitive chat: one answer per command per cooldown.
        now = time.monotonic()
        if now - self._last_cmd.get(cmd, -1e9) < self.cfg["command_cooldown_seconds"]:
            return
        self._last_cmd[cmd] = now
        if cmd == "!commands":
            names = sorted(set(commands) | {self.cfg["ask_command"], "!top"})
            self.say("Commands: " + " ".join(names))
        elif cmd == "!top":
            top = ", ".join(f"{n} ({c})" for n, c in self.chatters.most_common(3))
            self.say(f"Most active in chat right now: {top}. Thank you! 💜" if top else "Chat's just warming up!")
        else:
            self.say(self.fill(commands[cmd], name=name))

    # ---- !ask -----------------------------------------------------------
    def _allowed(self, key: str) -> bool:
        now = time.monotonic()
        if now - self._last_ask.get(key, -1e9) < self.cfg["ask_cooldown_seconds"]:
            return False
        user = self._asks_by_user[key]
        if sum(1 for t in user if now - t < 3600) >= self.cfg["ask_per_user_per_hour"]:
            return False
        if sum(1 for t in self._asks_all if now - t < 60) >= self.cfg["ask_per_minute"]:
            return False
        self._last_ask[key] = now
        user.append(now)
        self._asks_all.append(now)
        return True

    def faq_answer(self, question: str) -> str | None:
        q = normalise_question(question)
        best, score = None, 0.0
        for item in self.app.persona.get("faq") or []:
            answer = str(item.get("a") or "").strip()
            if not answer or answer.upper().startswith("TODO"):
                continue
            ratio = difflib.SequenceMatcher(None, q, normalise_question(item.get("q", ""))).ratio()
            if ratio > score:
                best, score = answer, ratio
        return best if score >= 0.72 else None

    def ask(self, name: str, key: str, question: str) -> None:
        if not question:
            self.say(f"@{name} ask me something after {self.cfg['ask_command']} 🙂")
            return
        if not self._allowed(key):
            return
        label = (self.cfg.get("ai_label") or "").strip()
        prefix = f"{label + ' ' if label else ''}@{name} "
        cache_key = hashlib.sha256(normalise_question(question).encode()).hexdigest()[:32]
        recent = [t for ts, t in self._recent_ai if time.monotonic() - ts < REPEAT_WINDOW_S]

        answer = self.faq_answer(question)
        source = "faq"
        if answer is None:
            answer = self.app.store.cache_get(cache_key, self.cfg["ask_cache_days"] * 86400)
            source = "cache"
        if answer is not None:
            self.app.ai_budget.record_cached()
        else:
            source = "ai"
            system = persona_prompt(self.app.persona) + (
                "\n\nYou are answering a viewer in your live stream chat"
                + (f" (stream: \"{self.title}\")" if self.title else "")
                + ". Reply in the same language the viewer used, in under 170 characters, plain text, friendly."
                + " If you don't know, say so honestly."
            )
            try:
                answer = self.app.llm.text(system, f"{name} asks: {question}", temperature=0.6)
            except LLMError as exc:
                log.error("!ask failed: %s", exc)
                return
            verdict = policy.full_check(self.app.llm, self.app.persona, prefix + answer, f"Live chat question from {name}: {question}")
            if verdict.block:
                log.warning("!ask answer not sent (%s)", verdict.summary())
                return
            self.app.store.cache_put(cache_key, question[:300], answer)

        message = (prefix + answer)[:200]
        if any("repetitive" in r for r in policy.check_text(message, self.app.persona, recent).review):
            # Same answer was just posted: point to it instead of repeating it (no repetitive chat).
            message, recent = f"{prefix}answered just above ☝️", []
        if self.say(message, recent=recent):
            self._recent_ai.append((time.monotonic(), message))
            bus.emit("ask", name=name, question=question[:200], answer=answer[:200])
            log.info("!ask answered from %s", source, extra={"event": "ask", "source": source})

    # ---- loop -----------------------------------------------------------
    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        resp = self.yt.live_messages(self.chat_id)  # first page = backlog; don't answer old messages
        token = resp.get("nextPageToken")
        wait = max(resp.get("pollingIntervalMillis", 5000) / 1000, self.cfg["poll_seconds_min"])
        log.info("Connected to live chat%s", f" for \"{self.title}\"" if self.title else "")
        live_state.active, live_state.title, live_state.chatters = True, self.title, 0
        health.ok("live_chat", f"connected: {self.title}"[:120])
        bus.emit("live", active=True, title=self.title)
        try:
            self._loop(stop, token, wait)
        finally:
            live_state.active = False
            health.ok("live_chat", "not live")
            bus.emit("live", active=False, title=self.title)
            if self.chatters:
                log.info("Top chatters this stream: %s", self.chatters.most_common(5))

    def _loop(self, stop: threading.Event, token, wait) -> None:
        if self.cfg.get("greeting") and "greeting" not in self._disabled_commands:
            self.say(self.fill(self.cfg["greeting"]))
        backoff = 0.0
        while not stop.wait(wait):
            try:
                resp = self.yt.live_messages(self.chat_id, token)
            except ApiError as exc:
                reason = error_reason(exc)
                if reason in STOP_REASONS:
                    log.info("Live chat stopped: %s", reason)
                    return
                if reason == "localQuotaBudget":
                    health.warn("live_chat", "paused: YouTube quota almost used up")
                    wait = 300
                    continue
                backoff = min(60.0, backoff * 2 or 5.0)
                health.warn("live_chat", f"reconnecting ({exc.status or 'network'})")
                log.warning("Live chat poll error (%s), retrying in %.0fs", reason or exc, backoff)
                wait = backoff
                continue
            if backoff:
                health.ok("live_chat", f"connected: {self.title}"[:120])
                backoff = 0.0
            token = resp.get("nextPageToken", token)
            mult = self.app.quota.poll_multiplier() if getattr(self.app, "quota", None) else 1.0
            wait = max(resp.get("pollingIntervalMillis", 5000) / 1000, self.cfg["poll_seconds_min"]) * mult
            for msg in resp.get("items", []):
                try:
                    self.handle(msg)
                except Exception:
                    log.exception("Error handling chat message")
            if resp.get("offlineAt"):
                log.info("Stream went offline.")
                return


class LiveWatcher:
    """Used by the service: checks for an active stream and runs the chat bot in a thread (restarts it if it dies)."""

    def __init__(self, app):
        self.app = app
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()

    def check(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        if self.app.quota.remaining() < 100:
            health.warn("live_chat", "YouTube quota too low to watch for streams")
            return
        found = self.app.yt.active_live_chat()
        if not found:
            if health.status("live_chat") in ("unknown", "error"):
                health.ok("live_chat", "not live")
            return
        chat_id, title = found
        bot = LiveChatBot(self.app, self.app.yt, chat_id, title)
        self.stop.clear()

        def guarded():
            try:
                bot.run(self.stop)
            except Exception as exc:
                health.fail("live_chat", f"crashed: {exc}"[:200])
                log.exception("Live chat bot crashed; it will restart on the next check")

        self.thread = threading.Thread(target=guarded, name="live-chat", daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.stop.set()
