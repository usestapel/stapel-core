"""
Telegram alerting — logging handler + direct send utility.

Env vars:
  TELEGRAM_BOT_TOKEN     — bot token from @BotFather
  TELEGRAM_ALERT_CHAT_ID — chat/channel ID (e.g. -1003839967873)
  TELEGRAM_ALERT_THREAD_ID — forum topic/thread ID (e.g. 1717), optional

Usage as logging handler (auto-sends ERROR+ to Telegram):
  Configured via LOGGING in common settings when env vars are set.

Direct usage:
  from stapel_core.django.monitoring.telegram import send_alert
  send_alert("Something broke: <details>")
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BOT_TOKEN = ""
_CHAT_ID = ""
_THREAD_ID: Optional[str] = None


def _cfg() -> tuple[str, str, Optional[str]]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", _BOT_TOKEN)
    chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID", _CHAT_ID)
    thread_id = os.getenv("TELEGRAM_ALERT_THREAD_ID", _THREAD_ID or "")
    return token, chat_id, thread_id or None


def is_configured() -> bool:
    token, chat_id, _ = _cfg()
    return bool(token and chat_id)


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    Send a message to the configured Telegram chat/topic.
    Returns True on success, False on any error (never raises).
    """
    token, chat_id, thread_id = _cfg()
    if not (token and chat_id):
        return False

    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = thread_id

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        # Never let Telegram errors bubble up into the app
        logger.debug("Telegram send failed: %s", exc)
        return False


def send_alert(text: str, service: str = "") -> bool:
    """
    Send a formatted alert message to the Telegram alert topic.

    Args:
        text: Alert text (HTML allowed)
        service: Service name for context (e.g. "stapel-auth")
    """
    prefix = f"🚨 <b>{service}</b>\n" if service else "🚨 <b>Alert</b>\n"
    return send_message(f"{prefix}{text}")


class TelegramHandler(logging.Handler):
    """
    Django logging handler that forwards ERROR+ records to Telegram.

    Add to LOGGING['handlers'] and attach to 'root' or specific loggers.
    Silently skips if TELEGRAM_BOT_TOKEN / TELEGRAM_ALERT_CHAT_ID are unset.
    """

    def __init__(self, service: str = "", level: int = logging.ERROR):
        super().__init__(level)
        self.service = service

    def emit(self, record: logging.LogRecord) -> None:
        if not is_configured():
            return
        try:
            msg = self.format(record)
            # Truncate to Telegram's 4096 char limit
            if len(msg) > 3800:
                msg = msg[:3800] + "\n… (truncated)"
            escaped = (
                msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            prefix = f"🚨 <b>{self.service or record.name}</b> [{record.levelname}]\n"
            send_message(f"{prefix}<pre>{escaped}</pre>")
        except Exception:
            self.handleError(record)
