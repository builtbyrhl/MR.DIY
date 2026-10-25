from __future__ import annotations

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from bot.db.repo import list_bookmarks, list_watched_lecture_ids
from bot.keyboards.inline import bookmarks_kb, browse_subjects_kb
from bot.services.lecture_display import decorate_lecture_titles
from bot.student_ui import (
    clamp_page,
    lecture_count_label,
    page_suffix,
)

router = Router()

BOOKMARKS_PER_PAGE = 8


async def show_bookmarks(message_or_cb, session, db_user, page: int = 0):
    bms = await list_bookmarks(session, db_user.id)
    if not bms:
        text = (
            "⭐ *Saved lectures*\n\n"
            "No bookmarks yet. Tap ☆ Bookmark on any lecture to save it here."
        )
        kb = browse_subjects_kb()
        if hasattr(message_or_cb, "message"):
            await message_or_cb.message.edit_text(
                text, parse_mode="Markdown", reply_markup=kb
            )
        else:
            await message_or_cb.answer(
                text, parse_mode="Markdown", reply_markup=kb
            )
        return

    await decorate_lecture_titles(session, bms)
    page = clamp_page(page, len(bms), BOOKMARKS_PER_PAGE)
    watched_ids = await list_watched_lecture_ids(session, db_user.id)
    text = (
        "⭐ *Saved lectures*\n"
        f"{lecture_count_label(len(bms))} saved."
        f"{page_suffix(page, len(bms), BOOKMARKS_PER_PAGE)}"
    )
    kb = bookmarks_kb(
        bms,
        page=page,
        per_page=BOOKMARKS_PER_PAGE,
        watched_ids=watched_ids,
    )
    if hasattr(message_or_cb, "message"):
        await message_or_cb.message.edit_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
    else:
        await message_or_cb.answer(
            text, parse_mode="Markdown", reply_markup=kb
        )


@router.message(F.text == "⭐ Bookmarks")
async def bookmarks_entry(message: Message, session, db_user):
    await show_bookmarks(message, session, db_user)


@router.message(Command("bookmarks"))
async def bookmarks_cmd(message: Message, session, db_user):
    await show_bookmarks(message, session, db_user)


@router.callback_query(F.data == "bookmarks")
async def bookmarks_callback(cb: CallbackQuery, session, db_user):
    await show_bookmarks(cb, session, db_user)
    await cb.answer()


@router.callback_query(F.data.startswith("bookmarks:"))
async def bookmarks_page_callback(cb: CallbackQuery, session, db_user):
    try:
        page = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Invalid page", show_alert=True)
        return
    await show_bookmarks(cb, session, db_user, page=page)
    await cb.answer()
