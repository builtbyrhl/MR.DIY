from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.repo import (
    active_streak,
    is_premium,
    list_reminder_recipients,
    mark_reminder_sent,
    set_reminder_enabled,
    up_next_lectures,
)
from bot.db.session import get_session_factory
from bot.student_ui import lecture_title

logger = logging.getLogger(__name__)

REMINDER_INTERVAL_SECONDS = 900


async def _build_reminder(session, user, today) -> tuple[str, InlineKeyboardMarkup | None]:
    streak = active_streak(user, today)
    picks = await up_next_lectures(
        session, user.id, limit=1, include_premium=await is_premium(user)
    )
    text = "⏰ Study reminder\n\n"
    if streak > 0:
        text += f"🔥 Keep your {streak}-day streak alive today."
    else:
        text += "📚 A quick lecture now keeps your study habit on track."
    if picks:
        subject, lecture = picks[0]
        text += (
            f"\n\nUp next: {subject.name} — {lecture_title(lecture)}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="▶️ Watch next", callback_data=f"lec:{lecture.id}"
                    )
                ]
            ]
        )
        return text, kb
    text += "\n\nPick any subject and watch one lecture."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Browse subjects", callback_data="subjects")]
        ]
    )
    return text, kb


async def send_daily_reminders(
    bot: Bot, *, now: datetime | None = None, session_factory=None
) -> int:
    """Send today's reminder to every opted-in user who has not had one."""
    now = now or datetime.utcnow()
    today = now.date()
    factory = session_factory or get_session_factory()
    sent = 0
    async with factory() as session:
        users = await list_reminder_recipients(session, today)
        for user in users:
            try:
                text, kb = await _build_reminder(session, user, today)
                await bot.send_message(
                    user.id, text, parse_mode=None, reply_markup=kb
                )
            except TelegramForbiddenError:
                await set_reminder_enabled(session, user.id, False)
                logger.info("Disabled reminders for user %s (bot blocked)", user.id)
                continue
            except TelegramRetryAfter as exc:
                logger.warning(
                    "Reminder rate limited at user %s (retry in %ss)",
                    user.id,
                    exc.retry_after,
                )
                continue
            except Exception:
                logger.warning(
                    "Failed to send reminder to user %s", user.id, exc_info=True
                )
                continue
            await mark_reminder_sent(session, user.id, today)
            sent += 1
            await asyncio.sleep(0.05)
    return sent


async def reminder_loop(bot: Bot, hour_utc: int) -> None:
    """Sweep for due reminders a few times per hour after the chosen hour."""
    while True:
        try:
            now = datetime.utcnow()
            if now.hour >= hour_utc:
                sent = await send_daily_reminders(bot, now=now)
                if sent:
                    logger.info("Daily reminders sent to %s users", sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Daily reminder sweep failed")
        await asyncio.sleep(REMINDER_INTERVAL_SECONDS)
