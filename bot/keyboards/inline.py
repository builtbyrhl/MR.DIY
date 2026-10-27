from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.student_ui import (
    clamp_page,
    clip,
    lecture_label,
    total_pages,
)


def _nav_rows(prefix: str, page: int, pages: int) -> list[list[InlineKeyboardButton]]:
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️ Previous", callback_data=f"{prefix}:{page - 1}"
            )
        )
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                text="Next ▶️", callback_data=f"{prefix}:{page + 1}"
            )
        )
    return [nav] if nav else []


def request_access_kb(pending: bool = False) -> InlineKeyboardMarkup:
    label = "⏳ Check request status" if pending else "🔓 Request Access"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data="request_access")],
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


def institutions_kb(institutions, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    pages = total_pages(len(institutions), per_page)
    page = clamp_page(page, len(institutions), per_page)
    start = page * per_page
    chunk = institutions[start:start + per_page]
    rows = [
        [
            InlineKeyboardButton(
                text=clip(f"🏛 {inst.name}"),
                callback_data=f"inst:{inst.id}:0",
            )
        ]
        for inst in chunk
    ]
    rows.extend(_nav_rows("instpage", page, pages))
    rows.append([InlineKeyboardButton(text="🔙 Home", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def institution_subjects_kb(
    subjects,
    institution_id: int,
    page: int = 0,
    per_page: int = 8,
    counts: dict[int, int] | None = None,
) -> InlineKeyboardMarkup:
    pages = total_pages(len(subjects), per_page)
    page = clamp_page(page, len(subjects), per_page)
    start = page * per_page
    chunk = subjects[start:start + per_page]
    counts = counts or {}
    rows = []
    for subject in chunk:
        label = f"{subject.emoji} {subject.name}"
        if subject.id in counts:
            label += f" · {counts[subject.id]}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=clip(label),
                    callback_data=f"instsubj:{institution_id}:{subject.slug}",
                )
            ]
        )
    rows.extend(_nav_rows(f"instsubjpage:{institution_id}", page, pages))
    rows.append([InlineKeyboardButton(text="🔙 Institutions", callback_data="institutions")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subjects_kb(
    subjects,
    page: int = 0,
    per_page: int = 8,
    counts: dict[int, int] | None = None,
) -> InlineKeyboardMarkup:
    pages = total_pages(len(subjects), per_page)
    page = clamp_page(page, len(subjects), per_page)
    start = page * per_page
    chunk = subjects[start:start + per_page]
    counts = counts or {}
    rows = []
    for subject in chunk:
        label = f"{subject.emoji} {subject.name}"
        if subject.id in counts:
            label += f" · {counts[subject.id]}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=clip(label),
                    callback_data=f"subj:{subject.slug}",
                )
            ]
        )
    rows.extend(_nav_rows("subjpage", page, pages))
    rows.append([InlineKeyboardButton(text="🔙 Home", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lectures_kb(
    lectures,
    subject_slug: str,
    page: int = 0,
    per_page: int = 8,
    watched_ids: set[int] | None = None,
) -> InlineKeyboardMarkup:
    pages = total_pages(len(lectures), per_page)
    page = clamp_page(page, len(lectures), per_page)
    start = page * per_page
    chunk = lectures[start:start + per_page]
    watched_ids = watched_ids or set()
    rows = [
        [
            InlineKeyboardButton(
                text=lecture_label(
                    lecture, watched=lecture.id in watched_ids
                ),
                callback_data=f"lec:{lecture.id}",
            )
        ]
        for lecture in chunk
    ]
    rows.extend(_nav_rows(f"lecpage:{subject_slug}", page, pages))
    rows.append(
        [InlineKeyboardButton(text="🔙 Subjects", callback_data="subjects")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def teachers_kb(
    groups,
    subject_id: int,
    page: int = 0,
    per_page: int = 8,
    has_topics: bool = False,
) -> InlineKeyboardMarkup:
    pages = total_pages(len(groups), per_page)
    page = clamp_page(page, len(groups), per_page)
    start = page * per_page
    chunk = groups[start:start + per_page]
    rows = [
        [
            InlineKeyboardButton(
                text=clip(f"{group.name} · {group.lecture_count}"),
                callback_data=f"tlec:{subject_id}:{group.teacher_id or 0}:0",
            )
        ]
        for group in chunk
    ]
    rows.extend(_nav_rows(f"teachers:{subject_id}", page, pages))
    if has_topics:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗂 Browse by topic",
                    callback_data=f"topics:{subject_id}:0",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🔙 Subjects", callback_data="subjects")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topics_kb(
    groups,
    subject_id: int,
    page: int = 0,
    per_page: int = 8,
) -> InlineKeyboardMarkup:
    pages = total_pages(len(groups), per_page)
    page = clamp_page(page, len(groups), per_page)
    start = page * per_page
    chunk = groups[start:start + per_page]
    rows = [
        [
            InlineKeyboardButton(
                text=clip(f"{group.name} · {group.lecture_count}"),
                callback_data=(
                    f"topicview:{subject_id}:{group.topic_id}:0"
                ),
            )
        ]
        for group in chunk
    ]
    rows.extend(_nav_rows(f"topics:{subject_id}", page, pages))
    rows.append(
        [
            InlineKeyboardButton(
                text="👩‍🏫 Browse by teacher",
                callback_data=f"teachers:{subject_id}:0",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="🔙 Subjects", callback_data="subjects")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def teacher_topics_kb(
    groups,
    subject_id: int,
    teacher_id: int,
    page: int = 0,
    per_page: int = 8,
) -> InlineKeyboardMarkup:
    pages = total_pages(len(groups), per_page)
    page = clamp_page(page, len(groups), per_page)
    start = page * per_page
    chunk = groups[start:start + per_page]
    rows = [
        [
            InlineKeyboardButton(
                text=clip(f"{group.name} · {group.lecture_count}"),
                callback_data=(
                    f"ttopic:{subject_id}:{teacher_id}:{group.topic_id}:0"
                ),
            )
        ]
        for group in chunk
    ]
    rows.extend(_nav_rows(f"tlec:{subject_id}:{teacher_id}", page, pages))
    rows.append(
        [
            InlineKeyboardButton(
                text="📖 All teacher lectures",
                callback_data=f"tlecflat:{subject_id}:{teacher_id}:0",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Teachers", callback_data=f"teachers:{subject_id}:0"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def teacher_topic_lectures_kb(
    lectures,
    subject_id: int,
    teacher_id: int,
    topic_id: int,
    page: int = 0,
    per_page: int = 8,
    watched_ids: set[int] | None = None,
) -> InlineKeyboardMarkup:
    pages = total_pages(len(lectures), per_page)
    page = clamp_page(page, len(lectures), per_page)
    start = page * per_page
    chunk = lectures[start:start + per_page]
    watched_ids = watched_ids or set()
    rows = [
        [
            InlineKeyboardButton(
                text=lecture_label(
                    lecture, watched=lecture.id in watched_ids
                ),
                callback_data=(
                    f"lec:{lecture.id}:tcht:{subject_id}:{teacher_id}:"
                    f"{topic_id}:{page}"
                ),
            )
        ]
        for lecture in chunk
    ]
    rows.extend(
        _nav_rows(
            f"ttopic:{subject_id}:{teacher_id}:{topic_id}", page, pages
        )
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Systems",
                callback_data=f"tlec:{subject_id}:{teacher_id}:0",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topic_lectures_kb(
    lectures,
    subject_id: int,
    topic_id: int,
    page: int = 0,
    per_page: int = 8,
    teacher_names: dict[int, str] | None = None,
    watched_ids: set[int] | None = None,
) -> InlineKeyboardMarkup:
    pages = total_pages(len(lectures), per_page)
    page = clamp_page(page, len(lectures), per_page)
    start = page * per_page
    chunk = lectures[start:start + per_page]
    teacher_names = teacher_names or {}
    watched_ids = watched_ids or set()
    rows = [
        [
            InlineKeyboardButton(
                text=lecture_label(
                    lecture,
                    teacher_name=teacher_names.get(lecture.teacher_id or 0),
                    watched=lecture.id in watched_ids,
                ),
                callback_data=(
                    f"lec:{lecture.id}:t:{subject_id}:{topic_id}:{page}"
                ),
            )
        ]
        for lecture in chunk
    ]
    rows.extend(_nav_rows(f"topicview:{subject_id}:{topic_id}", page, pages))
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Topics", callback_data=f"topics:{subject_id}:0"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def teacher_lectures_kb(
    lectures,
    subject_id: int,
    teacher_id: int,
    page: int = 0,
    per_page: int = 8,
    watched_ids: set[int] | None = None,
    nav_prefix: str = "tlec",
) -> InlineKeyboardMarkup:
    pages = total_pages(len(lectures), per_page)
    page = clamp_page(page, len(lectures), per_page)
    start = page * per_page
    chunk = lectures[start:start + per_page]
    watched_ids = watched_ids or set()
    rows = [
        [
            InlineKeyboardButton(
                text=lecture_label(
                    lecture, watched=lecture.id in watched_ids
                ),
                callback_data=(
                    f"lec:{lecture.id}:tch:{subject_id}:{teacher_id}:{page}"
                ),
            )
        ]
        for lecture in chunk
    ]
    rows.extend(
        _nav_rows(f"{nav_prefix}:{subject_id}:{teacher_id}", page, pages)
    )
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
        rows.append(
            [
                InlineKeyboardButton(
                    text="💎 Unlock with Premium", callback_data="premium"
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="▶️ Watch lecture", callback_data=f"watch:{lecture_id}"
                )
            ]
        )
    bm = "★ Remove bookmark" if is_bookmarked else "☆ Bookmark"
    rows.append([InlineKeyboardButton(text=bm, callback_data=f"bm:{lecture_id}")])
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ Mark as watched", callback_data=f"done:{lecture_id}"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bookmarks_kb(
    lectures,
    page: int = 0,
    per_page: int = 8,
    watched_ids: set[int] | None = None,
) -> InlineKeyboardMarkup:
    pages = total_pages(len(lectures), per_page)
    page = clamp_page(page, len(lectures), per_page)
    start = page * per_page
    chunk = lectures[start:start + per_page]
    watched_ids = watched_ids or set()
    rows = [
        [
            InlineKeyboardButton(
                text=lecture_label(
                    lecture, watched=lecture.id in watched_ids
                ),
                callback_data=f"lec:{lecture.id}:b:{page}",
            )
        ]
        for lecture in chunk
    ]
    rows.extend(_nav_rows("bookmarks", page, pages))
    rows.append(
        [InlineKeyboardButton(text="📚 Browse subjects", callback_data="subjects")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_results_kb(
    lectures,
    page: int = 0,
    per_page: int = 8,
) -> InlineKeyboardMarkup:
    pages = total_pages(len(lectures), per_page)
    page = clamp_page(page, len(lectures), per_page)
    start = page * per_page
    chunk = lectures[start:start + per_page]
    rows = [
        [
            InlineKeyboardButton(
                text=lecture_label(lecture),
                callback_data=f"lec:{lecture.id}:s",
            )
        ]
        for lecture in chunk
    ]
    rows.extend(_nav_rows("searchpage", page, pages))
    rows.append(
        [InlineKeyboardButton(text="📚 Browse subjects", callback_data="subjects")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def browse_subjects_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📚 Browse subjects", callback_data="subjects"
                )
            ]
        ]
    )


def pagination_kb(
    prefix: str, page: int, total_page_count: int
) -> InlineKeyboardMarkup:
    page = min(max(page, 0), max(total_page_count - 1, 0))
    rows = _nav_rows(prefix, page, total_page_count)
    return InlineKeyboardMarkup(inline_keyboard=rows)
