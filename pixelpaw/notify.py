"""Delivery: post reports and alerts to Discord, Telegram, and/or a local file."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .config import Settings
from .events import bus
from .net import ApiError, request

log = logging.getLogger(__name__)


def _chunks(text: str, size: int) -> list[str]:
    out, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if current:
                out.append(current)
                current = ""
            out.append(line[:size])
            line = line[size:]
        if len(current) + len(line) > size:
            out.append(current)
            current = ""
        current += line
    if current.strip():
        out.append(current)
    return out


def send_discord(settings: Settings, text: str) -> bool:
    if not settings.discord_webhook_url:
        return False
    for part in _chunks(text, 1900):
        request("POST", settings.discord_webhook_url, json_body={"content": part}, timeout=20)
    return True


def send_telegram(settings: Settings, text: str) -> bool:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    for part in _chunks(text, 4000):
        request("POST", url, json_body={"chat_id": settings.telegram_chat_id, "text": part}, timeout=20)
    return True


def save_file(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def deliver(settings: Settings, targets: list[str], filename: str, text: str) -> list[str]:
    """Send a report to each target. Returns the targets that succeeded."""
    done = []
    for target in targets:
        try:
            if target == "file":
                log.info("Report saved to %s", save_file(settings.reports_dir, filename, text))
                done.append(target)
            elif target == "discord" and send_discord(settings, text):
                done.append(target)
            elif target == "telegram" and send_telegram(settings, text):
                done.append(target)
        except ApiError as exc:
            log.error("Delivery to %s failed: %s", target, exc)
    return done


def alert(settings: Settings, text: str) -> None:
    """Instant alert (automations). Goes to the dashboard, the log, and Discord/Telegram if configured."""
    msg = f"🔔 [{datetime.now():%H:%M}] {text}"
    log.warning(msg)
    bus.emit("alert", text=text)
    for send in (send_discord, send_telegram):
        try:
            send(settings, msg)
        except ApiError as exc:
            log.error("Alert delivery failed: %s", exc)
