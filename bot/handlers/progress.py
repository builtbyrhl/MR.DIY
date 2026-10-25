from __future__ import annotations

from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command

from bot.db.repo import list_subjects, subject_progress, is_premium
from bot.keyboards.inline import browse_subjects_kb
from bot.services.catalog import ensure_subjects
from bot.student_ui import progress_bar, progress_percent

router = Router()


@router.message(F.text == "📈 Progress")
async def progress_entry(message: Message, session, db_user):
    await ensure_subjects(session)
    subjects = await list_subjects(session)
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

    if not lines:
        text = (
            "📈 *Your progress*\n\n"
            "No published lectures yet. Browse the library to get started."
        )
        await message.answer(
            text, parse_mode="Markdown", reply_markup=browse_subjects_kb()
        )
        return

    overall = progress_percent(total_done, total_lectures)
    text = (
        "📈 *Your progress*\n"
        f"{progress_bar(total_done, total_lectures)} "
        f"{total_done}/{total_lectures} · {overall}% overall\n\n"
        + "\n".join(lines)
    )
    premium = await is_premium(db_user)
    text += "\n\n" + ("💎 Premium active" if premium else "🆓 Free plan")
    await message.answer(
        text, parse_mode="Markdown", reply_markup=browse_subjects_kb()
    )


@router.message(Command("progress"))
async def progress_cmd(message: Message, session, db_user):
    await progress_entry(message, session, db_user)
