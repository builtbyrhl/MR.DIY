from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.fsm.context import FSMContext

from bot.keyboards.inline import browse_subjects_kb, request_access_kb
from bot.keyboards.reply import main_menu_kb
from bot.db.repo import is_premium, last_watched_lecture
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
    "🎓 Welcome to *FMGE Lecture Bot*\n\n"
    "Your organised medical lecture library.\n\n"
    "*How it works*\n"
    "1. Choose a subject from the menu.\n"
    "2. Pick a topic or teacher.\n"
    "3. Open a lecture, save it, and track your progress.\n\n"
    "Use the menu below whenever you are ready."
)

ACCESS_WELCOME = (
    "🎓 Welcome to *FMGE Lecture Bot*\n\n"
    "This library is private. Request access and an admin will review your "
    "account.\n\n"
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
    premium = await is_premium(db_user)
    text = WELCOME + ("\n\n💎 You have Premium access." if premium else "\n\n🆓 Free plan — upgrade for HD, notes & quizzes.")
    expires = access_expires_at(db_user)
    if expires is not None:
        text += (
            f"\n\n🔑 Access valid until "
            f"{format_access_expiry(expires)}."
        )
    await message.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())


@router.message(F.text == "▶️ Continue")
async def continue_learning(message: Message, session, db_user):
    lecture = await last_watched_lecture(session, db_user.id)
    if lecture is None:
        await message.answer(
            "▶️ Continue learning\n\n"
            "No lectures started yet. Browse your courses to begin.",
            parse_mode=None,
            reply_markup=browse_subjects_kb(),
        )
        return

    await decorate_lecture_titles(session, [lecture])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Resume lecture",
                    callback_data=f"lec:{lecture.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📚 Browse courses",
                    callback_data="subjects",
                )
            ],
        ]
    )
    await message.answer(
        f"▶️ Continue learning\n\n{lecture_title(lecture)}",
        parse_mode=None,
        reply_markup=keyboard,
    )


@router.message(Command("help"))
@router.message(F.text == "❓ Help")
async def help_msg(message: Message):
    await message.answer(
        "❓ *Help*\n\n"
        "*Browse*\n"
        "• 📚 My Courses — open a subject, then a topic or teacher\n"
        "• ▶️ Continue — resume your last opened lecture\n"
        "• 🔑 My Access — check your access and expiry\n"
        "• 🔍 Search — find a lecture by keyword\n\n"
        "*More commands*\n"
        "/bookmarks — saved lectures\n"
        "/progress — your progress\n"
        "/premium — upgrade\n"
        "/myaccess — access status\n"
        "/start — home\n"
        "/subjects — browse subjects\n"
        "/search <keyword> — find lectures\n"
        "/admin — admin panel (admins only)",
        parse_mode="Markdown",
    )


# subjects handled in subjects router
