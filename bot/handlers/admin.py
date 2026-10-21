from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.types import Message, ReplyParameters
from aiogram.filters import Command

from bot.config import get_settings
from bot.db.repo import grant_premium
from bot.db.session import get_session_factory

router = Router()
logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_ids


@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🛠 Admin commands:\n"
        "/addsubject <slug>|<name>|<emoji>\n"
        "/addlecture <subject_slug>|<title>|<channel_msg_id>|[premium]\n"
        "/reorder <subject_slug>\n"
        "/grant <user_id> [days]\n"
        "/stats\n\n"
        "Upload catalogue:\n"
        "/addcourse <year>|<subject_slug>|<name>|<aliases>|[dest]|[topic]\n"
        "/courses\n"
        "/review\n"
        "/batch <id> · /dryrun <id> · /approve <id>\n"
        "/skip <item> · /keep <item> · /retry <item>\n"
        "/pause · /resume\n"
        "/whereami"
    )


@router.message(Command("whereami"))
async def whereami_cmd(message: Message):
    sender = getattr(message, "from_user", None)
    if sender is None or not is_admin(sender.id):
        logger.warning(
            "Ignored /whereami: sender is not an admin (chat=%s sender=%s)",
            getattr(getattr(message, "chat", None), "id", None),
            getattr(sender, "id", None),
        )
        return
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    title = getattr(chat, "title", None) or getattr(chat, "type", "unknown")
    text = (
        f"chat.id: {chat.id}\n"
        f"message_thread_id: {thread_id if thread_id is not None else 'None'}\n"
        f"chat title: {title}"
    )
    # Reply to the command message itself so the answer stays in the same
    # forum topic (a plain chat message would land in the General topic).
    await message.answer(
        text,
        reply_parameters=ReplyParameters(
            message_id=message.message_id,
            allow_sending_without_reply=True,
        ),
    )


@router.message(Command("addsubject"))
async def add_subject(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    parts = [p.strip() for p in message.text.replace("/addsubject", "", 1).strip().split("|")]
    if len(parts) < 2:
        await message.answer("Usage: /addsubject slug|name|emoji")
        return
    slug, name = parts[0], parts[1]
    emoji = parts[2] if len(parts) > 2 else "📘"
    from bot.db.models import Subject
    session.add(Subject(slug=slug, name=name, emoji=emoji))
    await session.commit()
    await message.answer(f"Added subject {name}")


@router.message(Command("addlecture"))
async def add_lecture(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    parts = [p.strip() for p in message.text.replace("/addlecture", "", 1).strip().split("|")]
    if len(parts) < 3:
        await message.answer("Usage: /addlecture subject_slug|title|channel_msg_id|[premium]")
        return
    slug, title, msg_id = parts[0], parts[1], int(parts[2])
    premium = (parts[3].lower() == "premium") if len(parts) > 3 else False
    from bot.db.repo import get_subject
    subj = await get_subject(session, slug)
    if not subj:
        await message.answer("Subject not found")
        return
    from bot.db.models import Lecture
    from sqlalchemy import select, func
    max_order = await session.execute(select(func.coalesce(func.max(Lecture.order), 0)).where(Lecture.subject_id == subj.id))
    next_order = (max_order.scalar() or 0) + 1
    session.add(Lecture(subject_id=subj.id, title=title, channel_message_id=msg_id, is_premium=premium, order=next_order))
    await session.commit()
    await message.answer(f"Added lecture: {title}")


@router.message(Command("grant"))
async def grant_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /grant <user_id> [days]")
        return
    uid = int(args[1])
    days = int(args[2]) if len(args) > 2 else 365
    await grant_premium(session, uid, days)
    await message.answer(f"Granted premium to {uid} for {days} days")


@router.message(Command("reorder"))
async def reorder_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /reorder <subject_slug>")
        return
    from bot.db.repo import get_subject
    subj = await get_subject(session, args[1])
    if not subj:
        await message.answer("Subject not found")
        return
    from bot.db.models import Lecture
    from sqlalchemy import select
    res = await session.execute(
        select(Lecture).where(Lecture.subject_id == subj.id).order_by(Lecture.channel_message_id, Lecture.id)
    )
    lectures = list(res.scalars().all())
    for i, lec in enumerate(lectures, start=1):
        lec.order = i
    await session.commit()
    await message.answer(f"Reordered {len(lectures)} lectures in {subj.name}")


@router.message(Command("stats"))
async def stats_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    from sqlalchemy import select, func
    from bot.db.models import User, Lecture
    u = await session.execute(select(func.count(User.id)))
    l = await session.execute(select(func.count(Lecture.id)))
    await message.answer(f"Users: {u.scalar()}\nLectures: {l.scalar()}")
