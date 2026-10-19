from __future__ import annotations

import time
from typing import Callable, Awaitable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TgUser

from bot.config import get_settings


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate: float = 0.5):
        self.rate = rate
        self._last: Dict[int, float] = {}

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable], event: TelegramObject, data: dict) -> any:
        user: TgUser | None = data.get("event_from_user")
        if user is not None:
            now = time.monotonic()
            last = self._last.get(user.id, 0.0)
            if now - last < self.rate:
                return None
            self._last[user.id] = now
        return await handler(event, data)
