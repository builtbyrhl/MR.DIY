from __future__ import annotations

from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command

from bot.db.repo import list_subjects, subject_progress, is_premium
from bot.services.catalog import ensure_subjects

router = Router()


@router.message(F.text == "📈 Progress")
async def progress_entry(message: Message, session, db_user):
    await ensure_subjects(session)
    subjects = await list_subjects(session)
    lines = ["📈 *Your Progress*\n"]
    total_d = total_t = 0
    for s in subjects:
        d, t = await subject_progress(session, db_user.id, s.id)
        total_d += d
        total_t += t
        pct = int(d / t * 100) if t else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        lines.append(f"{s.emoji} {s.name}: {bar} {d}/{t}")
    overall = int(total_d / total_t * 100) if total_t else 0
    lines.insert(1, f"Overall: {total_d}/{total_t} ({overall}%)\n")
    premium = await is_premium(db_user)
    lines.append("\n" + ("💎 Premium active" if premium else "🆓 Free plan"))
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("progress"))
async def progress_cmd(message: Message, session, db_user):
    await progress_entry(message, session, db_user)
