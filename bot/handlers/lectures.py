from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.utils.markdown import markdown_decoration

from bot.config import get_settings
from bot.db.models import Subject, Teacher, UploadItem
from bot.db.repo import (
    add_bookmark,
    get_lecture,
    is_premium,
    list_bookmarks,
    mark_watched,
    remove_bookmark,
)
from bot.keyboards.inline import lecture_actions_kb
from bot.services.access import has_library_access
from bot.services.lecture_display import decorate_lecture_titles
from bot.services.media_delivery import record_delivered_media
from bot.student_ui import lecture_title

router = Router()
logger = logging.getLogger(__name__)

# track last subject for the legacy "back_subject" callback
_last_subject: dict[int, str] = {}
_lecture_back_callbacks: dict[int, str] = {}


def _lecture_back_callback(lecture) -> str:
    return f"back_teacher:{lecture.subject_id}:{lecture.teacher_id or 0}"


def _callback_back(lecture, callback_data: str) -> str:
    parts = callback_data.split(":")
    if len(parts) >= 7 and parts[2] == "tcht":
        return (
            f"back_teacher_topic:{parts[3]}:{parts[4]}:{parts[5]}:"
            f"{parts[6]}"
        )
    if len(parts) >= 6 and parts[2] == "t":
        return f"back_topic:{parts[3]}:{parts[4]}:{parts[5]}"
    if len(parts) >= 6 and parts[2] == "tch":
        return f"back_teacher:{parts[3]}:{parts[4]}:{parts[5]}"
    if len(parts) >= 3 and parts[2] == "b":
        page = parts[3] if len(parts) >= 4 and parts[3].isdigit() else "0"
        return f"bookmarks:{page}"
    if len(parts) >= 3 and parts[2] == "s":
        return "back_search"
    return _lecture_back_callback(lecture)


async def _lecture_header(session, lecture) -> str:
    title = markdown_decoration.quote(lecture_title(lecture))
    lines = [f"🎬 *{title}*"]
    subject = await session.get(Subject, lecture.subject_id)
    context = []
    if subject is not None:
        context.append(f"📚 {markdown_decoration.quote(subject.name)}")
    if lecture.order:
        context.append(f"Lecture {lecture.order}")
    if context:
        lines.append(" · ".join(context))
    if lecture.teacher_id is not None:
        teacher = await session.get(Teacher, lecture.teacher_id)
        if teacher is not None:
            lines.append(
                f"👩‍🏫 {markdown_decoration.quote(teacher.name)}"
            )
    return "\n".join(lines)


async def _lecture_text(session, lecture) -> str:
    text = await _lecture_header(session, lecture)
    if lecture.description:
        text += f"\n\n{markdown_decoration.quote(lecture.description)}"
    details = []
    if lecture.duration_sec:
        details.append(f"⏱ {lecture.duration_sec // 60} min")
    if lecture.tags:
        details.append(f"🏷 {markdown_decoration.quote(lecture.tags)}")
    if details:
        text += f"\n\n{' · '.join(details)}"
    return text


@router.callback_query(F.data.startswith("lec:"))
async def open_lecture(cb: CallbackQuery, session, db_user, bot):
    if not has_library_access(db_user):
        await cb.answer(
            "Library access not approved. Use /start to request access.",
            show_alert=True,
        )
        return
    lid = int(cb.data.split(":")[1])
    lec = await get_lecture(session, lid)
    if not lec:
        await cb.answer("Not found", show_alert=True)
        return
    await decorate_lecture_titles(session, [lec])
    back_callback = _callback_back(lec, cb.data)
    _lecture_back_callbacks[db_user.id] = back_callback
    premium = await is_premium(db_user)
    if lec.is_premium and not premium:
        text = await _lecture_header(session, lec)
        text += "\n\n💎 Premium lecture. Upgrade to unlock this lesson."
        await cb.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=lecture_actions_kb(
                lec.id,
                False,
                premium,
                True,
                back_callback=back_callback,
            ),
        )
        await cb.answer()
        return
    bm_list = await list_bookmarks(session, db_user.id)
    is_bm = any(b.id == lec.id for b in bm_list)
    subj = await session.get(Subject, lec.subject_id)
    if subj:
        _last_subject[db_user.id] = subj.slug
    text = await _lecture_text(session, lec)
    kb = lecture_actions_kb(
        lec.id,
        is_bm,
        premium,
        lec.is_premium,
        back_callback=back_callback,
    )
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("watch:"))
async def watch_lecture(cb: CallbackQuery, session, db_user, bot):
    if not has_library_access(db_user):
        await cb.answer(
            "Library access not approved. Use /start to request access.",
            show_alert=True,
        )
        return
    lid = int(cb.data.split(":")[1])
    lec = await get_lecture(session, lid)
    if not lec:
        await cb.answer("Media not available", show_alert=True)
        return

    premium = await is_premium(db_user)
    if lec.is_premium and not premium:
        await cb.answer(
            "Premium lecture. Upgrade to watch.",
            show_alert=True,
        )
        return

    upload = None
    if lec.source_upload_item_id is not None:
        upload = await session.get(UploadItem, lec.source_upload_item_id)
        if upload is None or not upload.file_id:
            await cb.answer("Media not available", show_alert=True)
            return
    elif not lec.channel_message_id:
        await cb.answer("Media not available", show_alert=True)
        return

    try:
        if upload is not None:
            if upload.media_type == "video":
                sent = await bot.send_video(
                    chat_id=cb.from_user.id,
                    video=upload.file_id,
                    caption=lec.title,
                    parse_mode=None,
                    protect_content=True,
                )
            elif upload.media_type == "document":
                sent = await bot.send_document(
                    chat_id=cb.from_user.id,
                    document=upload.file_id,
                    caption=lec.title,
                    parse_mode=None,
                    protect_content=True,
                )
            else:
                await cb.answer("Media not available", show_alert=True)
                return
        else:
            settings = get_settings()
            if not settings.channel_id:
                await cb.answer("Channel not configured", show_alert=True)
                return
            sent = await bot.copy_message(
                chat_id=cb.from_user.id,
                from_chat_id=settings.channel_id,
                message_id=lec.channel_message_id,
                protect_content=True,
            )
    except Exception:
        logger.exception("Failed to send lecture #%s", lid)
        await cb.answer(
            "Could not send this lecture. Please try again.",
            show_alert=True,
        )
        return

    try:
        await record_delivered_media(
            session,
            user_id=db_user.id,
            lecture_id=lec.id,
            chat_id=cb.from_user.id,
            message_id=getattr(sent, "message_id", None),
        )
    except Exception:
        logger.exception("Failed to track delivered lecture #%s", lid)
        await session.rollback()

    await mark_watched(session, db_user.id, lid)
    await cb.answer("Sent! Marked as watched ✅")


@router.callback_query(F.data.startswith("done:"))
async def mark_done(cb: CallbackQuery, session, db_user):
    lid = int(cb.data.split(":")[1])
    await mark_watched(session, db_user.id, lid)
    await cb.answer("Marked as watched ✅")


@router.callback_query(F.data.startswith("bm:"))
async def toggle_bm(cb: CallbackQuery, session, db_user):
    lid = int(cb.data.split(":")[1])
    lec = await get_lecture(session, lid)
    if lec is None:
        await cb.answer("Lecture not found", show_alert=True)
        return
    await decorate_lecture_titles(session, [lec])
    bms = await list_bookmarks(session, db_user.id)
    if any(b.id == lid for b in bms):
        await remove_bookmark(session, db_user.id, lid)
        await cb.answer("Removed from bookmarks")
    else:
        await add_bookmark(session, db_user.id, lid)
        await cb.answer("Saved to bookmarks ⭐")
    premium = await is_premium(db_user)
    bms = await list_bookmarks(session, db_user.id)
    is_bm = any(b.id == lid for b in bms)
    text = await _lecture_text(session, lec)
    kb = lecture_actions_kb(
        lec.id,
        is_bm,
        premium,
        lec.is_premium,
        back_callback=(
            _lecture_back_callbacks.get(db_user.id)
            or _lecture_back_callback(lec)
        ),
    )
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data == "back_subject")
async def back_subject(cb: CallbackQuery, session, db_user):
    slug = _last_subject.get(db_user.id)
    if not slug:
        await cb.message.edit_text("Use /subjects")
        await cb.answer()
        return
    from bot.db.repo import (
        get_subject,
        list_lectures,
        list_watched_lecture_ids,
        subject_progress,
    )
    from bot.keyboards.inline import lectures_kb
    from bot.student_ui import progress_line

    subject = await get_subject(session, slug)
    if subject is None:
        await cb.message.edit_text(
            "📚 Subject not found. Use /subjects to browse the library."
        )
        await cb.answer()
        return
    lectures = await list_lectures(session, subject.id, include_premium=True)
    d, t = await subject_progress(session, db_user.id, subject.id)
    watched_ids = await list_watched_lecture_ids(session, db_user.id)
    text = f"{subject.emoji} *{subject.name}*\n{progress_line(d, t)}"
    if lectures:
        text += "\n\nChoose a lecture to open it."
    kb = lectures_kb(
        lectures, subject.slug, page=0, watched_ids=watched_ids
    )
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await cb.answer()
