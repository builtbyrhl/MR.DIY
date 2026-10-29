from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.repo import (
    active_streak,
    count_watched_since,
    is_premium,
    list_digest_recipients,
    list_reminder_recipients,
    list_subjects,
    mark_digest_sent,
    mark_reminder_sent,
    set_digest_enabled,
    set_reminder_enabled,
    subject_progress,
    up_next_lectures,
)
from bot.db.session import get_session_factory
from bot.student_ui import (
    lecture_title,
    progress_bar,
    progress_percent,
)

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


async def _build_digest(session, user, today) -> tuple[str, InlineKeyboardMarkup]:
    premium = await is_premium(user)
    subjects = await list_subjects(session)
    total_done = total = 0
    subject_lines = []
    for subject in subjects:
        done, count = await subject_progress(session, user.id, subject.id)
        if count == 0:
            continue
        total_done += done
        total += count
        subject_lines.append(
            f"{subject.name}  {progress_bar(done, count)} {done}/{count}"
        )

    streak = active_streak(user, today)
    week_start = datetime.combine(today - timedelta(days=6), time.min)
    watched = await count_watched_since(session, user.id, week_start)
    picks = await up_next_lectures(
        session, user.id, limit=1, include_premium=premium
    )

    text = (
        "📬 Your week in review\n\n"
        f"🔥 Streak: {streak} day{'s' if streak != 1 else ''}\n"
        f"📚 Watched this week: {watched}\n"
    )
    if total:
        text += (
            f"📈 Overall: {total_done}/{total} "
            f"({progress_percent(total_done, total)}%)\n"
        )
    if subject_lines:
        text += "\n" + "\n".join(subject_lines[:6])

    if picks:
        subject, lecture = picks[0]
        text += f"\n\n🎯 Up next: {subject.name} — {lecture_title(lecture)}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="▶️ Continue", callback_data=f"lec:{lecture.id}"
                    )
                ]
            ]
        )
        return text, kb

    text += "\n\nPick a subject and start a new lecture this week."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Browse subjects", callback_data="subjects")]
        ]
    )
    return text, kb


async def send_weekly_digests(
    bot: Bot, *, now: datetime | None = None, session_factory=None
) -> int:
    """Send this week's digest to every opted-in user who has not had one."""
    now = now or datetime.utcnow()
    today = now.date()
    factory = session_factory or get_session_factory()
    sent = 0
    async with factory() as session:
        users = await list_digest_recipients(session, today)
        for user in users:
            try:
                text, kb = await _build_digest(session, user, today)
                await bot.send_message(
                    user.id, text, parse_mode=None, reply_markup=kb
                )
            except TelegramForbiddenError:
                await set_digest_enabled(session, user.id, False)
                logger.info("Disabled digest for user %s (bot blocked)", user.id)
                continue
            except TelegramRetryAfter as exc:
                logger.warning(
                    "Digest rate limited at user %s (retry in %ss)",
                    user.id,
                    exc.retry_after,
                )
                continue
            except Exception:
                logger.warning(
                    "Failed to send digest to user %s", user.id, exc_info=True
                )
                continue
            await mark_digest_sent(session, user.id, today)
            sent += 1
            await asyncio.sleep(0.05)
    return sent


async def digest_loop(bot: Bot, weekday: int, hour_utc: int) -> None:
    """Send the weekly digest on the configured weekday after the chosen hour."""
    while True:
        try:
            now = datetime.utcnow()
            if now.weekday() == weekday and now.hour >= hour_utc:
                sent = await send_weekly_digests(bot, now=now)
                if sent:
                    logger.info("Weekly digests sent to %s users", sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Weekly digest sweep failed")
        await asyncio.sleep(REMINDER_INTERVAL_SECONDS)
