from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Router
from aiogram.types import ChatMemberUpdated
from aiogram.filters import ChatMemberUpdatedFilter, MEMBER

from bot.config import get_settings
from bot.utils.telegram import chat_matches
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

router = Router()
logger = logging.getLogger(__name__)

STALE_JOIN_AFTER = timedelta(minutes=10)


def is_configured_chat(chat, settings=None) -> bool:
    settings = settings or get_settings()
    targets = {
        settings.onboarding_channel_id,
        settings.channel_id,
        settings.access_group_id,
    }
    return any(chat_matches(target, chat) for target in targets if target)


def is_stale_join(event: ChatMemberUpdated, now: datetime | None = None) -> bool:
    when = getattr(event, "date", None)
    if when is None:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now - when > STALE_JOIN_AFTER


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_new_member(event: ChatMemberUpdated):
    if is_stale_join(event):
        logger.info(
            "Skipping stale join welcome for %s in chat %s",
            event.new_chat_member.user.id,
            event.chat.id,
        )
        return
    settings = get_settings()
    if not is_configured_chat(event.chat, settings):
        logger.debug(
            "Skipping welcome for unconfigured chat %s", event.chat.id
        )
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Open Bot", url=f"https://t.me/{(await event.bot.get_me()).username}")],
    ])
    welcome_text = (
        f"👋 Welcome {user.full_name or user.first_name}!\n\n"
        "🎓 FMGE Lecture Bot is here to help you crack FMGE.\n"
        "Tap below to browse subjects, track progress and unlock premium."
    )
    try:
        if event.chat.type in ("group", "supergroup"):
            await event.bot.send_message(event.chat.id, f"👋 Welcome {user.full_name or user.first_name} to the community!", reply_markup=kb)
        else:
            await event.bot.send_message(user.id, welcome_text, reply_markup=kb)
    except Exception:
        logger.warning("Group welcome failed for chat %s", event.chat.id, exc_info=True)
        try:
            await event.bot.send_message(user.id, welcome_text, reply_markup=kb)
        except Exception:
            logger.warning("Could not DM welcome to user %s", user.id, exc_info=True)
