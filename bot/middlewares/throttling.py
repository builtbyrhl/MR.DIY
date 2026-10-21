from __future__ import annotations

import time
from typing import Callable, Awaitable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TgUser

from bot.config import get_settings
from bot.utils.telegram import chat_matches

# Diagnostic commands must always answer, even when the user just sent
# another message: dropping them silently looks like the bot is broken.
NEVER_THROTTLED_COMMANDS = {"whereami"}


def _is_never_throttled(event: TelegramObject) -> bool:
    text = getattr(event, "text", None)
    if not isinstance(text, str) or not text.startswith("/"):
        return False
    command = text.split()[0][1:].split("@")[0].lower()
    return command in NEVER_THROTTLED_COMMANDS


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate: float = 0.5):
        self.rate = rate
        self._last: Dict[int, float] = {}

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable], event: TelegramObject, data: dict) -> any:
        chat = getattr(event, "chat", None)
        if chat is None:
            chat = getattr(getattr(event, "message", None), "chat", None)
        if chat is not None and chat_matches(get_settings().upload_inbox_chat_id, chat):
            return await handler(event, data)

        if _is_never_throttled(event):
            return await handler(event, data)

        user: TgUser | None = data.get("event_from_user")
        if user is not None:
            now = time.monotonic()
            last = self._last.get(user.id, 0.0)
            if now - last < self.rate:
                return None
            self._last[user.id] = now
        return await handler(event, data)
