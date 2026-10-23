from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📚 Subjects"), KeyboardButton(text="🔍 Search")],
            [KeyboardButton(text="⭐ Bookmarks"), KeyboardButton(text="📈 Progress")],
            [KeyboardButton(text="💎 Premium"), KeyboardButton(text="❓ Help")],
        ],
        resize_keyboard=True,
    )


def request_access_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔓 Request Access", callback_data="request_access")],
        ]
    )


def access_review_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Approve", callback_data=f"access:approve:{user_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Reject", callback_data=f"access:reject:{user_id}"
                ),
            ]
        ]
    )


def subjects_kb(subjects, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = subjects[start:start + per_page]
    rows = [[InlineKeyboardButton(text=f"{s.emoji} {s.name}", callback_data=f"subj:{s.slug}")] for s in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"subjpage:{page-1}"))
    if start + per_page < len(subjects):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"subjpage:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lectures_kb(lectures, subject_slug: str, page: int = 0, per_page: int = 8, show_watched: bool = True) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = lectures[start:start + per_page]
    rows = []
    for lec in chunk:
        prefix = "✅ " if show_watched else ""
        label = f"{prefix}{lec.title}"
        if lec.is_premium:
            label = "💎 " + label
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"lec:{lec.id}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"lecpage:{subject_slug}:{page-1}"))
    if start + per_page < len(lectures):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"lecpage:{subject_slug}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Subjects", callback_data="subjects")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def teachers_kb(groups, subject_id: int, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = groups[start:start + per_page]
    rows = [
        [
            InlineKeyboardButton(
                text=f"{group.name} ({group.lecture_count})"[:60],
                callback_data=f"tlec:{subject_id}:{group.teacher_id or 0}:0",
            )
        ]
        for group in chunk
    ]
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️", callback_data=f"teachers:{subject_id}:{page-1}"
            )
        )
    if start + per_page < len(groups):
        nav.append(
            InlineKeyboardButton(
                text="▶️", callback_data=f"teachers:{subject_id}:{page+1}"
            )
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Subjects", callback_data="subjects")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def teacher_lectures_kb(
    lectures,
    subject_id: int,
    teacher_id: int,
    page: int = 0,
    per_page: int = 8,
) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = lectures[start:start + per_page]
    rows = []
    for lec in chunk:
        label = lec.title
        if lec.is_premium:
            label = "💎 " + label
        rows.append(
            [InlineKeyboardButton(text=label[:60], callback_data=f"lec:{lec.id}")]
        )
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️",
                callback_data=f"tlec:{subject_id}:{teacher_id}:{page-1}",
            )
        )
    if start + per_page < len(lectures):
        nav.append(
            InlineKeyboardButton(
                text="▶️",
                callback_data=f"tlec:{subject_id}:{teacher_id}:{page+1}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Teachers", callback_data=f"teachers:{subject_id}:0"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lecture_actions_kb(
    lecture_id: int,
    is_bookmarked: bool,
    is_premium_user: bool,
    lecture_premium: bool,
    back_callback: str = "back_subject",
) -> InlineKeyboardMarkup:
    rows = []
    if lecture_premium and not is_premium_user:
        rows.append([InlineKeyboardButton(text="💎 Unlock Premium", callback_data="premium")])
    else:
        rows.append([InlineKeyboardButton(text="▶️ Watch", callback_data=f"watch:{lecture_id}")])
    bm = "★ Remove bookmark" if is_bookmarked else "☆ Bookmark"
    rows.append([InlineKeyboardButton(text=bm, callback_data=f"bm:{lecture_id}")])
    rows.append([InlineKeyboardButton(text="✅ Mark watched", callback_data=f"done:{lecture_id}")])
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pagination_kb(prefix: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)
