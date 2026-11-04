"""Shared context passed to every routine. Clients are created lazily and rebuilt after settings change."""
from __future__ import annotations

import os
import threading
from dataclasses import replace

from .config import Settings, load_persona, load_settings
from .quota import AiBudget, QuotaTracker
from .store import Store

PLACEHOLDER_NAMES = {"", "My Channel", "YOUR CHANNEL NAME"}


class App:
    def __init__(self, settings: Settings | None = None):
        self._explicit_settings = settings is not None
        self.settings = settings or load_settings()
        self.store = Store(self.settings.db_path)
        self._lock = threading.RLock()
        self._llm = None
        self._yt = None
        self.yt_factory = None  # tests: a callable returning a YouTube client
        self.reload_persona()

    @property
    def overrides_file(self):
        return self.settings.db_path.parent / "persona.local.json"

    @property
    def llm(self):
        with self._lock:
            if self._llm is None:
                from .llm import LLM

                self._llm = LLM(self.settings, budget=self.ai_budget)
            return self._llm

    @property
    def yt(self):
        with self._lock:
            if self._yt is None:
                from .youtube import YouTube

                self._yt = self.yt_factory() if self.yt_factory else YouTube(self.settings, quota=self.quota)
            return self._yt

    def reload_persona(self) -> None:
        self.persona = load_persona(self.settings.persona_file, self.overrides_file)
        q, a = self.persona["quota"], self.persona["ai"]
        self.quota = QuotaTracker(self.store, q["daily_limit"], q["live_reserve"])
        self.ai_budget = AiBudget(self.store, a["max_calls_per_day"])
        with self._lock:
            if self._yt is not None:
                self._yt.quota = self.quota
            if self._llm is not None:
                self._llm.budget = self.ai_budget

    def reload(self) -> None:
        """Re-read .env + persona after the dashboard changed something."""
        fresh = load_settings()
        if self._explicit_settings:  # keep explicitly given paths/ports, refresh keys and options
            keep = {"client_secrets_file", "token_file", "db_path", "reports_dir", "output_dir", "persona_file",
                    "env_file", "data_dir", "web_port"}
            fresh = replace(fresh, **{k: getattr(self.settings, k) for k in keep})
        self.settings = fresh
        with self._lock:
            self._llm = None
            self._yt = None
        self.reload_persona()

    def setup_status(self) -> dict:
        s = self.settings
        base = os.getenv("OPENAI_BASE_URL") or ""
        local_ai = s.llm_provider == "openai" and ("localhost" in base or "127.0.0.1" in base)
        ai = bool(s.gemini_api_key) if s.llm_provider == "gemini" else bool(s.openai_api_key) or local_ai
        status = {
            "ai": ai,
            "client_secret": s.client_secrets_file.exists(),
            "youtube": s.token_file.exists(),
            "persona": self.persona["channel"]["name"] not in PLACEHOLDER_NAMES,
        }
        status["complete"] = all(status.values())
        return status

    def youtube_ready(self) -> bool:
        st = self.setup_status()
        return st["ai"] and st["youtube"]
