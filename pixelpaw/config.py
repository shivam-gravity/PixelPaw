"""Settings (from .env) and persona (from persona.yaml).

Settings = secrets and machine-specific paths.
Persona  = everything that makes the bot *yours*: voice, FAQ, links, rules.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(os.getenv("PIXELPAW_HOME") or Path.cwd()).resolve()
if not (ROOT / "persona.yaml").exists() and (Path(__file__).resolve().parent.parent / "persona.yaml").exists():
    ROOT = Path(__file__).resolve().parent.parent  # running from a source checkout


def load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=value, # comments, optional quotes). Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        os.environ.setdefault(key, value)

# Defaults for every persona key, so a partial persona.yaml still works.
DEFAULT_PERSONA: dict = {
    "channel": {
        "name": "My Channel",
        "handle": "",
        "niche": "",
        "audience": "",
        "language": "English",
    },
    "voice": {
        "tone": "friendly, genuine, concise",
        "style_rules": [],
        "never": [
            "Promise sponsorships, collaborations, giveaways or dates",
            "Share personal information",
            "Argue with critics",
            "Invent facts about the video or the creator",
        ],
        "signature": "",
        "max_reply_chars": 280,
        "reply_in_commenter_language": True,
        "examples": [],
    },
    "faq": [],
    "links": {},
    "comments": {
        "mode": "review",  # review | auto
        "reply_to": ["question", "praise", "feedback"],
        "review_negative": True,
        "max_replies_per_run": 20,
        "max_replies_per_day": 60,
        "reply_gap_seconds": 20,
        "min_comment_length": 3,
        "lookback_hours": 72,
        "max_pages": 3,
        "check_every_minutes": 30,
        "skip_authors": [],
    },
    "live_chat": {
        "auto_detect": True,
        "detect_every_minutes": 3,
        "poll_seconds_min": 6,
        "greeting": "",
        "ask_command": "!ask",
        "ask_cooldown_seconds": 30,
        "min_seconds_between_bot_messages": 4,
        "command_cooldown_seconds": 30,
        "ai_label": "🤖",
        "commands": {},
        "banned_words": [],
        "moderation_action": "delete",  # delete | ignore
        "delete_viewer_links": True,
        "thank_super_chats": True,
        "super_chat_thanks": "Thank you {name} for the {amount} Super Chat! 🙏",
        "new_member_thanks": "Welcome to the membership, {name}! 🎉",
    },
    "report": {
        "time": "09:00",
        "deliver_to": ["file"],
        "sweep_comments_first": True,
        "recent_uploads": 5,
    },
    "automations": [],
    "ui": {
        "language": "en",  # report, overlay and dashboard language (any code; see `translate-ui`)
        "overlay": {
            "show": ["super_chat", "member", "ask"],
            "seconds": 8,
            "position": "bottom-left",
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out


@dataclass
class Settings:
    llm_provider: str
    openai_api_key: str
    openai_model: str
    gemini_api_key: str
    gemini_model: str
    client_secrets_file: Path
    token_file: Path
    db_path: Path
    reports_dir: Path
    output_dir: Path
    persona_file: Path
    timezone: str
    discord_webhook_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    web_host: str
    web_port: int


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    return p if p.is_absolute() else ROOT / p


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    return Settings(
        llm_provider=os.getenv("LLM_PROVIDER", "gemini").strip().lower(),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        client_secrets_file=_path("YT_CLIENT_SECRETS", "client_secret.json"),
        token_file=_path("YT_TOKEN_FILE", "data/token.json"),
        db_path=_path("PIXELPAW_DB", "data/pixelpaw.sqlite3"),
        reports_dir=_path("PIXELPAW_REPORTS_DIR", "reports"),
        output_dir=_path("PIXELPAW_OUTPUT_DIR", "output"),
        persona_file=_path("PIXELPAW_PERSONA", "persona.yaml"),
        timezone=os.getenv("PIXELPAW_TIMEZONE", "Asia/Kolkata"),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        web_host=(os.getenv("PIXELPAW_WEB_HOST") or "127.0.0.1"),
        web_port=int(os.getenv("PIXELPAW_WEB_PORT") or "8765"),
    )


def load_persona(path: Path) -> dict:
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _deep_merge(DEFAULT_PERSONA, data)
