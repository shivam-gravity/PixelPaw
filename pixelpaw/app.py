"""Shared context passed to every routine. Clients are created lazily."""
from __future__ import annotations

from .config import Settings, load_persona, load_settings
from .store import Store


class App:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.persona = load_persona(self.settings.persona_file)
        self.store = Store(self.settings.db_path)
        self._llm = None
        self._yt = None

    @property
    def llm(self):
        if self._llm is None:
            from .llm import LLM

            self._llm = LLM(self.settings)
        return self._llm

    @property
    def yt(self):
        if self._yt is None:
            from .youtube import YouTube

            self._yt = YouTube(self.settings)
        return self._yt

    def reload_persona(self) -> None:
        self.persona = load_persona(self.settings.persona_file)
