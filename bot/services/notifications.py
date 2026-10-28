from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Lecture, Subject
from bot.db.repo import list_subject_follower_ids, unfollow_subject
from bot.student_ui import lecture_title

logger = logging.getLogger(__name__)


async def notify_subject_followers(
    session: AsyncSession, bot: Bot, lecture: Lecture
) -> int:
    """Send a new-lecture alert to every follower of the lecture's subject.

    Returns the number of alerts delivered. Users who blocked the bot are
    silently unfollowed so they stop receiving alerts.
    """
    follower_ids = await list_subject_follower_ids(session, lecture.subject_id)
    if not follower_ids:
        return 0

    subject = await session.get(Subject, lecture.subject_id)
    if subject is None:
        return 0

    title = lecture_title(lecture)
    order = getattr(lecture, "order", None)
    if order:
        title += f" · L{order}"
    text = (
        f"📚 New lecture in {subject.name}\n"
        f"{title}\n\n"
        "Tap below to open the subject and watch it."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Open subject", callback_data=f"subj:{subject.slug}"
                )
            ]
        ]
    )

    sent = 0
    for user_id in follower_ids:
        try:
            await bot.send_message(user_id, text, parse_mode=None, reply_markup=kb)
            sent += 1
        except TelegramForbiddenError:
            await unfollow_subject(session, user_id, lecture.subject_id)
        except TelegramRetryAfter as exc:
            logger.warning(
                "Follower alert rate limited for user %s (retry in %ss)",
                user_id,
                exc.retry_after,
            )
        except Exception:
            logger.warning("Follower alert failed for user %s", user_id, exc_info=True)
    return sent
