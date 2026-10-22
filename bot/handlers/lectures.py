from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.db.models import UploadItem
from bot.db.repo import get_lecture, is_watched, mark_watched, add_bookmark, remove_bookmark, is_premium
from bot.keyboards.inline import lecture_actions_kb

router = Router()
logger = logging.getLogger(__name__)

# track last subject for "back"
_last_subject: dict[int, str] = {}


@router.callback_query(F.data.startswith("lec:"))
async def open_lecture(cb: CallbackQuery, session, db_user, bot):
    lid = int(cb.data.split(":")[1])
    lec = await get_lecture(session, lid)
    if not lec:
        await cb.answer("Not found", show_alert=True)
        return
    premium = await is_premium(db_user)
    if lec.is_premium and not premium:
        await cb.message.edit_text(
            f"💎 *{lec.title}*\n\nPremium lecture. Upgrade to watch.",
            parse_mode="Markdown",
            reply_markup=lecture_actions_kb(lec.id, False, premium, True),
        )
        await cb.answer()
        return
    bm_list = await __import__("bot.db.repo", fromlist=["list_bookmarks"]).list_bookmarks(session, db_user.id)
    is_bm = any(b.id == lec.id for b in bm_list)
    from bot.db.models import Subject
    subj = await session.get(Subject, lec.subject_id)
    if subj:
        _last_subject[db_user.id] = subj.slug
    text = f"🎬 *{lec.title}*\n\n{lec.description or ''}\n"
    if lec.duration_sec:
        text += f"\n⏱ {lec.duration_sec//60} min"
    if lec.tags:
        text += f"\n🏷 {lec.tags}"
    kb = lecture_actions_kb(lec.id, is_bm, premium, lec.is_premium)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("watch:"))
async def watch_lecture(cb: CallbackQuery, session, db_user, bot):
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
                await bot.send_video(
                    chat_id=cb.from_user.id,
                    video=upload.file_id,
                    caption=lec.title,
                    parse_mode=None,
                )
            elif upload.media_type == "document":
                await bot.send_document(
                    chat_id=cb.from_user.id,
                    document=upload.file_id,
                    caption=lec.title,
                    parse_mode=None,
                )
            else:
                await cb.answer("Media not available", show_alert=True)
                return
        else:
            settings = get_settings()
            if not settings.channel_id:
                await cb.answer("Channel not configured", show_alert=True)
                return
            await bot.copy_message(
                chat_id=cb.from_user.id,
                from_chat_id=settings.channel_id,
                message_id=lec.channel_message_id,
            )
    except Exception:
        logger.exception("Failed to send lecture #%s", lid)
        await cb.answer(
            "Could not send this lecture. Please try again.",
            show_alert=True,
        )
        return
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
    # check existing
    from bot.db.repo import list_bookmarks
    bms = await list_bookmarks(session, db_user.id)
    if any(b.id == lid for b in bms):
        await remove_bookmark(session, db_user.id, lid)
        await cb.answer("Removed bookmark")
    else:
        await add_bookmark(session, db_user.id, lid)
        await cb.answer("Bookmarked ⭐")
    # refresh
    lec = await get_lecture(session, lid)
    premium = await is_premium(db_user)
    bms = await list_bookmarks(session, db_user.id)
    is_bm = any(b.id == lid for b in bms)
    text = f"🎬 *{lec.title}*\n\n{lec.description or ''}"
    kb = lecture_actions_kb(lec.id, is_bm, premium, lec.is_premium)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data == "back_subject")
async def back_subject(cb: CallbackQuery, session, db_user):
    slug = _last_subject.get(db_user.id)
    if not slug:
        await cb.message.edit_text("Use /subjects")
        await cb.answer()
        return
    from bot.db.repo import get_subject, list_lectures, subject_progress
    subject = await get_subject(session, slug)
    lectures = await list_lectures(session, subject.id, include_premium=True)
    d, t = await subject_progress(session, db_user.id, subject.id)
    pct = int(d / t * 100) if t else 0
    text = f"{subject.emoji} *{subject.name}*\nProgress: {d}/{t} ({pct}%)"
    from bot.keyboards.inline import lectures_kb
    kb = lectures_kb(lectures, subject.slug, page=0)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await cb.answer()
