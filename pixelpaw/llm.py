"""One small interface over Gemini and any OpenAI-compatible API (OpenAI, Ollama, OpenRouter, Groq…).

Plain REST over the standard library: no SDKs. Busy models are retried, then fallbacks are tried in order.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

from .config import Settings
from .health import health
from .net import ApiError, request

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TRANSIENT = {0, 408, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, settings: Settings, budget=None):
        self.budget = budget  # quota.AiBudget: daily cap + usage counts (optional)
        self.provider = settings.llm_provider
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS") or "45")
        if self.provider == "gemini":
            if not settings.gemini_api_key:
                raise LLMError("GEMINI_API_KEY is not set in .env")
            self.key = settings.gemini_api_key
            self.model = settings.gemini_model
            fallbacks = os.getenv("GEMINI_FALLBACK_MODELS", "gemini-flash-latest,gemini-3.8-flash")
        elif self.provider == "openai":
            self.base_url = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
            local = "localhost" in self.base_url or "127.0.0.1" in self.base_url
            if not settings.openai_api_key and not local:
                raise LLMError("OPENAI_API_KEY is not set in .env")
            self.key = settings.openai_api_key or "local"
            self.model = settings.openai_model
            fallbacks = os.getenv("OPENAI_FALLBACK_MODELS", "")
        else:
            raise LLMError(f"LLM_PROVIDER must be 'gemini' or 'openai', got {self.provider!r}")
        self.models = list(dict.fromkeys([self.model] + [m.strip() for m in fallbacks.split(",") if m.strip()]))
        self._benched: dict[str, float] = {}

    def text(self, system: str, prompt: str, temperature: float = 0.7) -> str:
        return self._generate(system, prompt, temperature, as_json=False).strip()

    def json(self, system: str, prompt: str, temperature: float = 0.4) -> dict:
        return parse_json(self._generate(system, prompt, temperature, as_json=True))

    def _generate(self, system: str, prompt: str, temperature: float, as_json: bool) -> str:
        if self.budget:
            self.budget.check()
        try:
            result = self._generate_inner(system, prompt, temperature, as_json)
        except LLMError as exc:
            if self.budget:
                self.budget.record(ok=False)
            health.fail("ai", str(exc)[:200])
            raise
        if self.budget:
            self.budget.record(ok=True)
        health.ok("ai", f"{self.provider}")
        return result

    def _generate_inner(self, system: str, prompt: str, temperature: float, as_json: bool) -> str:
        last: Exception | None = None
        now = time.time()
        # Models that failed recently go to the back of the line for 10 minutes.
        order = sorted(self.models, key=lambda m: self._benched.get(m, 0) > now)
        for model in order:
            for attempt in range(2):
                try:
                    result = self._call(model, system, prompt, temperature, as_json)
                    self._benched.pop(model, None)
                    return result
                except ApiError as exc:
                    last = exc
                    if exc.status == 404:
                        log.warning("Model %s not available, trying the next one", model)
                        self._benched[model] = time.time() + 86400
                        break
                    if exc.status in TRANSIENT:
                        if attempt == 0 and exc.status != 0:
                            time.sleep(2)
                            continue
                        log.warning("Model %s busy (%s), trying the next one", model, exc.status)
                        self._benched[model] = time.time() + 600
                        break
                    raise LLMError(f"{self.provider} request failed: {exc}") from exc
        raise LLMError(f"{self.provider} request failed: {last}")

    def _call(self, model: str, system: str, prompt: str, temperature: float, as_json: bool) -> str:
        if self.provider == "gemini":
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "responseMimeType": "application/json" if as_json else "text/plain",
                },
            }
            data = request("POST", GEMINI_URL.format(model=model), json_body=body,
                           headers={"x-goog-api-key": self.key}, timeout=self.timeout)
            cands = data.get("candidates") or []
            if not cands:
                reason = (data.get("promptFeedback") or {}).get("blockReason", "no answer")
                raise LLMError(f"Gemini returned nothing ({reason})")
            parts = (cands[0].get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))

        body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if as_json:
            body["response_format"] = {"type": "json_object"}
        data = request("POST", f"{self.base_url}/chat/completions", json_body=body,
                       headers={"Authorization": f"Bearer {self.key}"}, timeout=self.timeout)
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def parse_json(raw: str) -> dict:
    """Parse model output as JSON, tolerating code fences and stray prose."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"Model did not return JSON: {raw[:200]!r}")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model returned invalid JSON: {raw[:200]!r}") from exc
    if not isinstance(data, dict):
        raise LLMError("Model returned JSON that is not an object")
    return data
