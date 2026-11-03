"""Interface languages for reports, the OBS overlay and the dashboard.

Bundled: see pixelpaw/locales/*.json. Any other language: `python -m pixelpaw translate-ui <code>`
uses your AI model to create the file. Missing keys always fall back to English.
Comment and chat replies don't need this: the bot already replies in each viewer's own language.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

LOCALES = Path(__file__).resolve().parent / "locales"
RTL = {"ar", "he", "fa", "ur", "ps", "sd", "yi", "dv", "ug", "ckb", "ku"}


def _norm(lang: str) -> str:
    return (lang or "en").strip().replace("_", "-").lower()


@lru_cache(maxsize=64)
def strings(lang: str) -> dict:
    base = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    code = _norm(lang)
    for candidate in dict.fromkeys([code, code.split("-")[0]]):
        path = LOCALES / f"{candidate}.json"
        if candidate != "en" and path.exists():
            base.update(json.loads(path.read_text(encoding="utf-8")))
            break
    base["_dir"] = "rtl" if code.split("-")[0] in RTL else "ltr"
    base["_lang"] = code
    return base


def t(lang: str, key: str, **kwargs) -> str:
    text = strings(lang).get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def available() -> list[str]:
    return sorted(p.stem for p in LOCALES.glob("*.json"))


def translate_ui(llm, lang: str, language_name: str = "") -> Path:
    """Create pixelpaw/locales/<lang>.json from English with the AI model."""
    source = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    payload = {k: v for k, v in source.items() if not k.startswith("_")}
    prompt = f"""Translate the values of this JSON object into the language with code "{lang}"{f' ({language_name})' if language_name else ''}.
Rules: keep every key unchanged; keep placeholders like {{channel}} {{date}} {{day}} exactly; keep text inside `backticks`
unchanged; keep emoji; use natural, short UI wording a YouTuber would use. Also set "_name" to the language's own name.
Return only the JSON object.

{json.dumps(payload, ensure_ascii=False, indent=1)}"""
    data = llm.json("You are a professional software localiser.", prompt, temperature=0.2)
    out = {"_name": str(data.get("_name") or language_name or lang)}
    for key, value in payload.items():
        new = data.get(key)
        if isinstance(new, str) and set(re.findall(r"\{\w+\}", new)) == set(re.findall(r"\{\w+\}", value)):
            out[key] = new  # keep only translations that preserved their placeholders
    path = LOCALES / f"{_norm(lang)}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    strings.cache_clear()
    return path
