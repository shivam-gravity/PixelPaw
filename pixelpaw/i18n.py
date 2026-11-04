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
            loc = json.loads(path.read_text(encoding="utf-8"))
            app = {**base.get("app", {}), **(loc.pop("app", None) or {})}
            base.update(loc)
            base["app"] = app
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


def _translate_chunk(llm, lang: str, language_name: str, chunk: dict) -> dict:
    prompt = f"""Translate the values of this JSON object into the language with code "{lang}"{f' ({language_name})' if language_name else ''}.
Rules: keep every key unchanged; keep placeholders like {{channel}} {{date}} {{day}} {{name}} exactly; keep text inside
`backticks`, URLs, "OBS", "YouTube", "PixelPaw", "!ask" and file names unchanged; keep emoji; use natural, short UI wording
a YouTuber would use. Also add "_name": the language's own name. Return only the JSON object.

{json.dumps(chunk, ensure_ascii=False, indent=1)}"""
    return llm.json("You are a professional software localiser.", prompt, temperature=0.2)


def translate_ui(llm, lang: str, language_name: str = "") -> Path:
    """Create (or complete) pixelpaw/locales/<lang>.json from English with the AI model."""
    source = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    top = {k: v for k, v in source.items() if not k.startswith("_") and isinstance(v, str)}
    app_src = source.get("app", {})
    out: dict = {"_name": language_name or lang}
    out_app: dict = {}

    def keep(src: dict, got: dict, into: dict) -> None:
        for key, value in src.items():
            new = got.get(key)
            if isinstance(new, str) and new.strip() and set(re.findall(r"\{\w+\}", new)) == set(re.findall(r"\{\w+\}", value)):
                into[key] = new  # keep only translations that preserved their placeholders

    data = _translate_chunk(llm, lang, language_name, top)
    out["_name"] = str(data.get("_name") or out["_name"])
    keep(top, data, out)
    items = list(app_src.items())
    for start in range(0, len(items), 60):
        chunk = dict(items[start:start + 60])
        keep(chunk, _translate_chunk(llm, lang, language_name, chunk), out_app)
    out["app"] = out_app
    path = LOCALES / f"{_norm(lang)}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    strings.cache_clear()
    return path
