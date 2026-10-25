from __future__ import annotations

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from bot.db.repo import search_lectures
from bot.keyboards.inline import browse_subjects_kb, search_results_kb
from bot.student_ui import (
    clamp_page,
    lecture_count_label,
    page_suffix,
)

router = Router()

SEARCH_PER_PAGE = 8
_search_state: dict[int, tuple[str, int]] = {}


async def _render_search(message_or_cb, session, user_id: int, query: str, page: int):
    results = await search_lectures(session, query, limit=100)
    if not results:
        _search_state[user_id] = (query, 0)
        text = (
            "🔍 Search results\n"
            f'No lectures matched "{query}".\n\n'
            "Try a shorter keyword, or browse the library by subject."
        )
        if hasattr(message_or_cb, "message"):
            await message_or_cb.message.edit_text(
                text, parse_mode=None, reply_markup=browse_subjects_kb()
            )
        else:
            await message_or_cb.answer(
                text, parse_mode=None, reply_markup=browse_subjects_kb()
            )
        return

    page = clamp_page(page, len(results), SEARCH_PER_PAGE)
    _search_state[user_id] = (query, page)
    text = (
        "🔍 Search results\n"
        f'"{query}"\n\n'
        f"{lecture_count_label(len(results))} found."
        f"{page_suffix(page, len(results), SEARCH_PER_PAGE)}"
    )
    kb = search_results_kb(
        results, page=page, per_page=SEARCH_PER_PAGE
    )
    if hasattr(message_or_cb, "message"):
        await message_or_cb.message.edit_text(
            text, parse_mode=None, reply_markup=kb
        )
    else:
        await message_or_cb.answer(text, parse_mode=None, reply_markup=kb)


@router.message(F.text == "🔍 Search")
async def search_prompt(message: Message):
    await message.answer(
        "🔍 Search the library\n\n"
        "Send /search followed by a keyword.\n"
        "Example: /search anatomy heart",
        parse_mode=None,
    )


@router.message(Command("search"))
async def search_cmd(message: Message, session, db_user):
    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        await message.answer(
            "Usage: /search <keyword>\nExample: /search anatomy heart",
            parse_mode=None,
        )
        return
    await _render_search(message, session, db_user.id, query, 0)


@router.callback_query(F.data.startswith("searchpage:"))
async def search_page_callback(cb: CallbackQuery, session, db_user):
    state = _search_state.get(db_user.id)
    if state is None:
        await cb.answer("Search again to refresh results", show_alert=True)
        return
    try:
        page = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Invalid page", show_alert=True)
        return
    await _render_search(cb, session, db_user.id, state[0], page)
    await cb.answer()


@router.callback_query(F.data == "back_search")
async def back_search(cb: CallbackQuery, session, db_user):
    state = _search_state.get(db_user.id)
    if state is None:
        await cb.answer("Search again to refresh results", show_alert=True)
        return
    await _render_search(cb, session, db_user.id, state[0], state[1])
    await cb.answer()
