from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from bot.db.session import get_session_factory
from bot.db.repo import get_or_create_user


class DbMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        factory = get_session_factory()
        async with factory() as session:
            data["session"] = session
            tg_user = data.get("event_from_user")
            if tg_user is not None:
                data["db_user"] = await get_or_create_user(session, tg_user)
            result = await handler(event, data)
            await session.commit()
            return result
