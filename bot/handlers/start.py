from __future__ import annotations

from datetime import datetime

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.fsm.context import FSMContext
from aiogram.utils.markdown import markdown_decoration

from bot.keyboards.inline import browse_subjects_kb, request_access_kb
from bot.keyboards.reply import main_menu_kb
from bot.db.models import Subject
from bot.db.repo import (
    active_streak,
    first_lecture_in_subject,
    is_premium,
    last_watched_lecture,
    list_pinned_subjects,
    list_subjects,
    up_next_lectures,
)
from bot.services.access import (
    APPROVED,
    PENDING,
    REJECTED,
    access_expires_at,
    access_is_expired,
    access_status,
    format_access_expiry,
    has_library_access,
)
from bot.services.catalog import ensure_subjects
from bot.services.lecture_display import decorate_lecture_titles
from bot.student_ui import lecture_title


router = Router()


WELCOME = (
    "🎓 *FMGE Lecture Bot*\n"
    "Your organised medical lecture library."
)

ACCESS_WELCOME = (
    "🎓 Welcome to *FMGE Lecture Bot*\n\n"
    "This library is private. Request access and an admin will review your "
    "account.\n\n"
)

SEARCH_PROMPT = (
    "🔍 *Search the library*\n\n"
    "Send /search followed by a keyword.\n"
    "Example: /search anatomy heart"
)

HELP_TEXT = (
    "❓ *Help*\n\n"
    "*Everyday use*\n"
    "• 📚 Library — all subjects, pinned first\n"
    "• ▶️ Continue — jump to your next lecture\n"
    "• 📈 Progress — streaks, totals and history\n"
    "• ⚙️ Settings — access, search, institutions, saved, help\n\n"
    "*Commands*\n"
    "/bookmarks — saved lectures\n"
    "/progress — your progress\n"
    "/premium — upgrade\n"
    "/myaccess — access status\n"
    "/start — home\n"
    "/subjects — browse subjects\n"
    "/search <keyword> — find lectures\n"
    "/admin — admin panel (admins only)"
)

SETTINGS_TEXT = (
    "⚙️ *Settings*\n\n"
    "Access, search and saved items live here."
)


def _access_welcome_text(user) -> str:
    status = access_status(user)
    if status == APPROVED and access_is_expired(user):
        detail = (
            "⏳ Your library access expired on "
            f"{format_access_expiry(access_expires_at(user))}. "
            "You can request a renewal."
        )
    elif status == PENDING:
        detail = "⏳ Your access request is pending admin approval."
    elif status == REJECTED:
        detail = (
            "❌ Your previous access request was not approved. "
            "You can request access again."
        )
    else:
        detail = "🔒 Library access has not been approved yet."
    return ACCESS_WELCOME + detail


def _resume_kb(lecture_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Resume lecture",
                    callback_data=f"lec:{lecture_id}",
                )
            ],
            [
                InlineKeyboardButton(text="📚 Library", callback_data="subjects"),
                InlineKeyboardButton(text="📈 Progress", callback_data="progress"),
            ],
        ]
    )


def _settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔑 My access", callback_data="settings:access"),
                InlineKeyboardButton(text="🔍 Search", callback_data="settings:search"),
            ],
            [
                InlineKeyboardButton(text="🏛 Institutions", callback_data="institutions"),
                InlineKeyboardButton(text="⭐ Saved", callback_data="settings:bookmarks"),
            ],
            [InlineKeyboardButton(text="❓ Help", callback_data="settings:help")],
            [InlineKeyboardButton(text="🏠 Home", callback_data="home")],
        ]
    )


def _with_back(markup: InlineKeyboardMarkup | None, target: str = "settings:menu", label: str = "⚙️ Settings") -> InlineKeyboardMarkup:
    rows = list(markup.inline_keyboard) if markup else []
    rows.append([InlineKeyboardButton(text=label, callback_data=target)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _continue_target(session, db_user):
    premium = await is_premium(db_user)
    up_next = await up_next_lectures(
        session, db_user.id, limit=1, include_premium=premium
    )
    if up_next:
        subject, lecture = up_next[0]
        return subject, lecture, True
    lecture = await last_watched_lecture(session, db_user.id)
    if lecture is not None:
        subject = await session.get(Subject, lecture.subject_id)
        if subject is not None:
            return subject, lecture, True
    pinned = await list_pinned_subjects(session, db_user.id)
    for subject in pinned or await list_subjects(session):
        first = await first_lecture_in_subject(
            session, subject.id, include_premium=premium
        )
        if first is not None:
            return subject, first, False
    return None


async def build_home(session, db_user) -> tuple[str, InlineKeyboardMarkup]:
    premium = await is_premium(db_user)
    streak = active_streak(db_user, datetime.utcnow().date())
    target = await _continue_target(session, db_user)

    if target is not None:
        subject, lecture, started = target
        await decorate_lecture_titles(session, [lecture])
        heading = "Continue where you left off" if started else "Start here"
        text = (
            f"{WELCOME}\n\n"
            f"▶️ *{heading}*\n"
            f"{subject.emoji} {markdown_decoration.quote(subject.name)} — "
            f"{markdown_decoration.quote(lecture_title(lecture))}"
        )
        kb = _resume_kb(lecture.id)
    else:
        text = (
            f"{WELCOME}\n\n"
            "Pick a subject below and your lectures will appear here."
        )
        kb = browse_subjects_kb()

    if streak > 1:
        text += f"\n\n🔥 {streak}-day streak — keep it going!"
    if premium:
        text += "\n\n💎 Premium active"
    expires = access_expires_at(db_user)
    if expires is not None:
        text += (
            f"\n\n🔑 Access valid until "
            f"{format_access_expiry(expires)}."
        )
    return text, kb


@router.message(CommandStart())
async def cmd_start(message: Message, session, db_user):
    if not has_library_access(db_user):
        status = access_status(db_user)
        pending = status == PENDING and db_user.access_requested_at is not None
        await message.answer(
            _access_welcome_text(db_user),
            parse_mode="Markdown",
            reply_markup=request_access_kb(pending=pending),
        )
        return

    await ensure_subjects(session)
    text, kb = await build_home(session, db_user)
    await message.answer(
        text, parse_mode="Markdown", reply_markup=main_menu_kb()
    )
    await message.answer(
        "Quick start", parse_mode=None, reply_markup=kb
    )


@router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery, session, db_user):
    await ensure_subjects(session)
    text, kb = await build_home(session, db_user)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await cb.answer()


@router.message(F.text == "▶️ Continue")
async def continue_learning(message: Message, session, db_user):
    target = await _continue_target(session, db_user)
    if target is None:
        await message.answer(
            "▶️ Continue learning\n\n"
            "No lectures started yet. Browse your library to begin.",
            parse_mode=None,
            reply_markup=browse_subjects_kb(),
        )
        return

    subject, lecture, started = target
    await decorate_lecture_titles(session, [lecture])
    heading = "▶️ Continue learning" if started else "▶️ Start learning"
    await message.answer(
        f"{heading}\n\n{subject.emoji} {subject.name}\n"
        f"{lecture_title(lecture)}",
        parse_mode=None,
        reply_markup=_resume_kb(lecture.id),
    )


@router.message(Command("settings"))
@router.message(F.text == "⚙️ Settings")
async def settings_entry(message: Message, db_user):
    await message.answer(
        SETTINGS_TEXT, parse_mode="Markdown", reply_markup=_settings_kb()
    )


@router.callback_query(F.data == "settings:menu")
async def cb_settings_menu(cb: CallbackQuery):
    await cb.message.edit_text(
        SETTINGS_TEXT, parse_mode="Markdown", reply_markup=_settings_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "settings:access")
async def cb_settings_access(cb: CallbackQuery, db_user):
    from bot.handlers.access import my_access_view

    text, markup = my_access_view(db_user)
    await cb.message.edit_text(
        text, parse_mode="Markdown", reply_markup=_with_back(markup)
    )
    await cb.answer()


@router.callback_query(F.data == "settings:search")
async def cb_settings_search(cb: CallbackQuery):
    await cb.message.edit_text(
        SEARCH_PROMPT,
        parse_mode="Markdown",
        reply_markup=_with_back(None, target="subjects", label="📚 Library"),
    )
    await cb.answer()


@router.callback_query(F.data == "settings:bookmarks")
async def cb_settings_bookmarks(cb: CallbackQuery, session, db_user):
    from bot.handlers.bookmarks import show_bookmarks

    await show_bookmarks(cb, session, db_user)
    await cb.answer()


@router.callback_query(F.data == "settings:help")
async def cb_settings_help(cb: CallbackQuery):
    await cb.message.edit_text(
        HELP_TEXT, parse_mode="Markdown", reply_markup=_with_back(None)
    )
    await cb.answer()


@router.callback_query(F.data == "lib:search")
async def cb_lib_search(cb: CallbackQuery):
    await cb.message.edit_text(
        SEARCH_PROMPT,
        parse_mode="Markdown",
        reply_markup=_with_back(None, target="subjects", label="📚 Library"),
    )
    await cb.answer()


@router.callback_query(F.data == "lib:saved")
async def cb_lib_saved(cb: CallbackQuery, session, db_user):
    from bot.handlers.bookmarks import show_bookmarks

    await show_bookmarks(cb, session, db_user)
    await cb.answer()


@router.message(Command("help"))
@router.message(F.text == "❓ Help")
async def help_msg(message: Message):
    await message.answer(HELP_TEXT, parse_mode="Markdown")


# subjects handled in subjects router
