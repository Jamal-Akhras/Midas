"""Telegram delivery for operator messages.

The bot token travels in the URL path, so nothing here logs a URL or an
exception's text: failures are reported by status code or exception class
only. Messages are plain text; long ones are split on line breaks under
Telegram's 4096-character limit.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4000  # Telegram allows 4096; headroom keeps the split simple


def _chunks(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        while len(line) > limit:  # a single oversized line is cut hard
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        extra = len(line) + (1 if current else 0)
        if size + extra > limit:
            chunks.append("\n".join(current))
            current, size = [line], len(line)
        else:
            current.append(line)
            size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_message(
    text: str,
    *,
    token: str,
    chat_id: str,
    timeout: float = 15.0,
    parse_mode: str | None = None,
) -> bool:
    """Post `text` to the chat. True only when every chunk was accepted.
    `parse_mode="HTML"` renders Telegram's HTML subset; the caller escapes."""
    if not token or not chat_id:
        logger.warning("telegram: bot token or chat id not configured; nothing sent")
        return False
    url = f"{API_BASE}/bot{token}/sendMessage"
    for chunk in _chunks(text):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            response = httpx.post(url, json=payload, timeout=timeout)
        except Exception as exc:
            logger.warning("telegram send failed: %s", type(exc).__name__)
            return False
        if response.status_code != 200 or not _ok(response):
            logger.warning("telegram send rejected: HTTP %s", response.status_code)
            return False
    return True


def discover_chat(*, token: str, timeout: float = 15.0) -> dict[str, str] | None:
    """The chat behind the most recent message the bot received, so the
    operator never has to find their chat id by hand: message the bot once,
    then run `midas notify setup`."""
    try:
        response = httpx.get(f"{API_BASE}/bot{token}/getUpdates", timeout=timeout)
    except Exception as exc:
        logger.warning("telegram getUpdates failed: %s", type(exc).__name__)
        return None
    if response.status_code != 200:
        logger.warning("telegram getUpdates rejected: HTTP %s", response.status_code)
        return None
    found: dict[str, str] | None = None
    for update in _result(response):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            name = chat.get("first_name") or chat.get("username") or chat.get("title") or ""
            found = {"chat_id": str(chat["id"]), "name": str(name)}
    return found


def _ok(response: httpx.Response) -> bool:
    try:
        return bool(response.json().get("ok"))
    except ValueError:
        return False


def _result(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        body = response.json()
    except ValueError:
        return []
    result = body.get("result") if isinstance(body, dict) else None
    return [u for u in result if isinstance(u, dict)] if isinstance(result, list) else []


def send_rich(html_text: str, plain_text: str, *, token: str, chat_id: str) -> bool:
    """Send the HTML rendering; if Telegram refuses it (a formatting slip),
    send the plain text instead so the operator still hears from the system."""
    if send_message(html_text, token=token, chat_id=chat_id, parse_mode="HTML"):
        return True
    logger.warning("telegram: HTML message refused, sending plain text")
    return send_message(plain_text, token=token, chat_id=chat_id)
