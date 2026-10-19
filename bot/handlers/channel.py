from __future__ import annotations

from aiogram import Router
from aiogram.types import ChatMemberUpdated
from aiogram.filters import ChatMemberUpdatedFilter, MEMBER, RESTRICTED

from bot.config import get_settings
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

router = Router()


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_new_member(event: ChatMemberUpdated):
    settings = get_settings()
    # only handle configured channel
    if settings.channel_id and str(event.chat.id) != str(settings.channel_id) and str(event.chat.username) != settings.channel_id.lstrip("@"):
        # allow if channel_id matches username/id loosely
        pass
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
        try:
            await event.bot.send_message(user.id, welcome_text, reply_markup=kb)
        except Exception:
            pass
