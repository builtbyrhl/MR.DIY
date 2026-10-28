from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.filters import Command
from aiogram.utils.markdown import markdown_decoration

from bot.db.repo import (
    active_streak,
    count_watched,
    count_watched_since,
    is_premium,
    list_subjects,
    set_reminder_enabled,
    subject_progress,
    up_next_lectures,
)
from bot.keyboards.inline import browse_subjects_kb
from bot.services.catalog import ensure_subjects
from bot.student_ui import clip, lecture_title, progress_bar, progress_percent

router = Router()
logger = logging.getLogger(__name__)


def _up_next_label(subject, lecture) -> str:
    label = f"▶️ {subject.name}"
    if lecture.order:
        label += f" · L{lecture.order}"
    return clip(label)


def _progress_kb(up_next, reminder_enabled: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=_up_next_label(subject, lecture),
                callback_data=f"lec:{lecture.id}",
            )
        ]
        for subject, lecture in up_next
    ]
    rows.extend(browse_subjects_kb().inline_keyboard)
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "🔔 Daily reminder: ON"
                    if reminder_enabled
                    else "🔕 Daily reminder: OFF"
                ),
                callback_data="reminder:toggle",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_progress(session, db_user) -> tuple[str, InlineKeyboardMarkup]:
    await ensure_subjects(session)
    subjects = await list_subjects(session)
    premium = await is_premium(db_user)
    total_done = total_lectures = 0
    lines = []
    for subject in subjects:
        done, total = await subject_progress(session, db_user.id, subject.id)
        if total == 0:
            continue
        total_done += done
        total_lectures += total
        lines.append(
            f"{subject.emoji} {subject.name}  "
            f"{progress_bar(done, total)} {done}/{total}"
        )

    up_next = await up_next_lectures(
        session, db_user.id, limit=3, include_premium=premium
    )
    reminder_enabled = bool(getattr(db_user, "reminder_enabled", False))
    kb = _progress_kb(up_next, reminder_enabled)

    if not lines:
        return (
            "📈 *Your progress*\n\n"
            "No published lectures yet. Browse the library to get started.",
            kb,
        )

    overall = progress_percent(total_done, total_lectures)
    today = datetime.utcnow().date()
    streak = active_streak(db_user, today)
    total_watched = await count_watched(session, db_user.id)
    watched_today = await count_watched_since(
        session, db_user.id, datetime.combine(today, time.min)
    )
    watched_week = await count_watched_since(
        session,
        db_user.id,
        datetime.combine(today - timedelta(days=6), time.min),
    )
    text = (
        "📈 *Your progress*\n"
        f"{progress_bar(total_done, total_lectures)} "
        f"{total_done}/{total_lectures} · {overall}% overall\n\n"
        f"🔥 Study streak: {streak} day{'s' if streak != 1 else ''}\n"
        f"📚 Watched: {total_watched} total · {watched_today} today · "
        f"{watched_week} this week\n\n"
        + "\n".join(lines)
    )
    if up_next:
        text += "\n\n🎯 *Up next*"
        for subject, lecture in up_next:
            text += (
                f"\n{subject.emoji} {markdown_decoration.quote(subject.name)} — "
                f"{markdown_decoration.quote(lecture_title(lecture))}"
            )
    text += "\n\n" + ("💎 Premium active" if premium else "🆓 Free plan")
    return text, kb


@router.message(F.text == "📈 Progress")
async def progress_entry(message: Message, session, db_user):
    text, kb = await build_progress(session, db_user)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


@router.message(Command("progress"))
async def progress_cmd(message: Message, session, db_user):
    await progress_entry(message, session, db_user)


async def _toggle_reminder(session, db_user) -> bool:
    enabled = not bool(getattr(db_user, "reminder_enabled", False))
    await set_reminder_enabled(session, db_user.id, enabled)
    db_user.reminder_enabled = enabled
    return enabled


@router.callback_query(F.data == "reminder:toggle")
async def reminder_toggle(cb: CallbackQuery, session, db_user):
    enabled = await _toggle_reminder(session, db_user)
    text, kb = await build_progress(session, db_user)
    try:
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramBadRequest as exc:
        logger.debug("Reminder toggle could not edit progress message: %s", exc)
    await cb.answer(
        "🔔 Daily reminder on — I'll nudge you every evening."
        if enabled
        else "🔕 Daily reminder off."
    )


@router.message(Command("reminder"))
async def reminder_cmd(message: Message, session, db_user):
    enabled = await _toggle_reminder(session, db_user)
    await message.answer(
        "🔔 Daily reminder on — I'll nudge you every evening."
        if enabled
        else "🔕 Daily reminder off."
    )
