from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from bot.config import get_settings
from bot.db.session import get_session_factory
from bot.db.repo import get_or_create_user
from bot.keyboards.inline import request_access_kb
from bot.services.access import (
    PENDING,
    REJECTED,
    access_status,
    has_library_access,
)
from bot.services.auth import load_db_admin_cache
from bot.utils.telegram import chat_matches

_ACCESS_EXEMPT_COMMANDS = {"start", "help"}
_ACCESS_REQUIRED_TEXT = "Library access required. Use /start to request access."


def _access_denial(user) -> tuple[str, bool]:
    status = access_status(user)
    if status == PENDING and getattr(user, "access_requested_at", None):
        return (
            "⏳ Your access request is awaiting admin review. "
            "You will be notified when it is approved.",
            True,
        )
    if status == REJECTED:
        return (
            "🔒 Your previous access request was not approved. "
            "Use /start to request access again.",
            False,
        )
    return _ACCESS_REQUIRED_TEXT, False


def _command_name(text: str | None) -> str | None:
    if not isinstance(text, str) or not text.startswith("/"):
        return None
    return text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()


def _event_chat(event: TelegramObject):
    return getattr(event, "chat", None) or getattr(
        getattr(event, "message", None), "chat", None
    )


def _is_callback(event: TelegramObject) -> bool:
    return hasattr(event, "data")


def _is_access_exempt(event: TelegramObject) -> bool:
    if _is_callback(event) and getattr(event, "data", None) == "request_access":
        return True

    chat = _event_chat(event)
    if chat is not None and chat_matches(
        get_settings().upload_inbox_chat_id, chat
    ):
        return True

    text = getattr(event, "text", None)
    if text == "❓ Help":
        return True
    return _command_name(text) in _ACCESS_EXEMPT_COMMANDS


async def _deny_access(event: TelegramObject, user) -> None:
    text, pending = _access_denial(user)
    if _is_callback(event):
        await event.answer(text, show_alert=True)
        return
    await event.answer(
        text,
        parse_mode=None,
        reply_markup=request_access_kb(pending=pending),
    )


class DbMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        factory = get_session_factory()
        async with factory() as session:
            data["session"] = session
            await load_db_admin_cache(session)
            tg_user = data.get("event_from_user")
            if tg_user is not None:
                db_user = await get_or_create_user(session, tg_user)
                data["db_user"] = db_user
                if not _is_access_exempt(event) and not has_library_access(
                    db_user
                ):
                    await _deny_access(event, db_user)
                    await session.commit()
                    return None

            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result
