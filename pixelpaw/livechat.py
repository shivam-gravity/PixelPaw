"""Live chat bot: !commands, !ask (AI answers in your voice), Super Chat thanks, banned-word moderation."""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque

from .net import ApiError

from . import policy
from .events import bus, live_state
from .intelligence import persona_prompt
from .llm import LLMError
from .youtube import error_reason

log = logging.getLogger(__name__)

STOP_REASONS = {"liveChatEnded", "liveChatNotFound", "liveChatDisabled", "forbidden", "quotaExceeded"}


class _Blank(dict):
    def __missing__(self, key):
        return ""


class LiveChatBot:
    def __init__(self, app, yt, chat_id: str, title: str = ""):
        self.app, self.yt, self.chat_id, self.title = app, yt, chat_id, title
        self.cfg = app.persona["live_chat"]
        self.my_id = yt.channel_id
        self.chatters: Counter = Counter()
        self._last_ask: dict[str, float] = {}
        self._last_cmd: dict[str, float] = {}
        self._last_sent = 0.0
        self._recent_ai: deque[str] = deque(maxlen=40)
        links = {k: v for k, v in (app.persona.get("links") or {}).items() if v}
        self._vars = _Blank(links, channel=app.persona["channel"]["name"], title=title)
        self._disabled_commands = self._validate_templates()

    def _validate_templates(self) -> set[str]:
        """Check the fixed messages in persona.yaml against the guidelines once, at startup."""
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
        commands = {k.lower(): v for k, v in (self.cfg.get("commands") or {}).items() if k.lower() not in self._disabled_commands}

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

    def ask(self, name: str, key: str, question: str) -> None:
        if not question:
            self.say(f"@{name} ask me something after {self.cfg['ask_command']} 🙂")
            return
        now = time.monotonic()
        if now - self._last_ask.get(key, -1e9) < self.cfg["ask_cooldown_seconds"]:
            return
        self._last_ask[key] = now
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
        label = (self.cfg.get("ai_label") or "").strip()
        message = f"{label + ' ' if label else ''}@{name} {answer}"[:200]
        verdict = policy.full_check(self.app.llm, self.app.persona, message, f"Live chat question from {name}: {question}")
        if verdict.block:
            log.warning("!ask answer not sent (%s)", verdict.summary())
            return
        if self.say(message, recent=list(self._recent_ai)):
            self._recent_ai.append(message)
            bus.emit("ask", name=name, question=question[:200], answer=answer[:200])

    # ---- loop -----------------------------------------------------------
    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        resp = self.yt.live_messages(self.chat_id)  # first page = backlog; don't answer old messages
        token = resp.get("nextPageToken")
        wait = max(resp.get("pollingIntervalMillis", 5000) / 1000, self.cfg["poll_seconds_min"])
        log.info("Connected to live chat%s", f" for \"{self.title}\"" if self.title else "")
        live_state.active, live_state.title, live_state.chatters = True, self.title, 0
        bus.emit("live", active=True, title=self.title)
        try:
            self._loop(stop, token, wait)
        finally:
            live_state.active = False
            bus.emit("live", active=False, title=self.title)

    def _loop(self, stop: threading.Event, token, wait) -> None:
        if self.cfg.get("greeting") and "greeting" not in self._disabled_commands:
            self.say(self.fill(self.cfg["greeting"]))

        while not stop.wait(wait):
            try:
                resp = self.yt.live_messages(self.chat_id, token)
            except ApiError as exc:
                reason = error_reason(exc)
                if reason in STOP_REASONS:
                    log.info("Live chat stopped: %s", reason)
                    break
                log.warning("Live chat poll error (%s), backing off", reason or exc)
                wait = 30
                continue
            token = resp.get("nextPageToken", token)
            wait = max(resp.get("pollingIntervalMillis", 5000) / 1000, self.cfg["poll_seconds_min"])
            for msg in resp.get("items", []):
                try:
                    self.handle(msg)
                except Exception:
                    log.exception("Error handling chat message")
            if resp.get("offlineAt"):
                log.info("Stream went offline.")
                break
        if self.chatters:
            log.info("Top chatters this stream: %s", self.chatters.most_common(5))


class LiveWatcher:
    """Used by `run`: checks for an active stream and starts the chat bot in a thread."""

    def __init__(self, app):
        self.app = app
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()

    def check(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        yt = self.app.yt  # the REST client is thread-safe
        found = yt.active_live_chat()
        if not found:
            return
        chat_id, title = found
        bot = LiveChatBot(self.app, yt, chat_id, title)
        self.stop.clear()
        self.thread = threading.Thread(target=bot.run, args=(self.stop,), name="live-chat", daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.stop.set()
