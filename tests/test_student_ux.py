from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio

from bot.db.models import Lecture, Subject, User
from bot.db.repo import add_bookmark, add_teacher, mark_watched
from bot.handlers.bookmarks import show_bookmarks
from bot.handlers.lectures import open_lecture
from bot.handlers.progress import progress_entry
from bot.handlers.search import back_search, search_cmd, search_page_callback
from bot.handlers.start import continue_learning
from bot.handlers.subjects import (
    open_subject,
    open_teacher_lectures,
    show_subjects,
)
from bot.middlewares.db import _access_denial
from bot.services.access import APPROVED, PENDING, REJECTED
from bot.student_ui import lecture_label, resolve_lecture_title


@pytest_asyncio.fixture
async def ux_catalogue(session):
    subject = Subject(
        slug="ux-anatomy", name="UX Anatomy", emoji="🫀", order=1
    )
    user = User(
        id=9901,
        username="uxstudent",
        full_name="UX Student",
        is_premium=False,
        is_admin=False,
        access_status=APPROVED,
    )
    session.add_all([subject, user])
    await session.commit()
    await session.refresh(subject)
    await session.refresh(user)
    return subject, user


async def _lectures(session, subject_id, titles, teacher_id=None):
    lectures = [
        Lecture(
            subject_id=subject_id,
            title=title,
            order=index,
            teacher_id=teacher_id,
        )
        for index, title in enumerate(titles, start=1)
    ]
    session.add_all(lectures)
    await session.commit()
    for lecture in lectures:
        await session.refresh(lecture)
    return lectures


def _message(user_id, text=""):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


def _callback(data, user_id=9901):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(edit_text=AsyncMock(), text=""),
        answer=AsyncMock(),
    )


def _button_data(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def _button_labels(markup):
    return [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]


def test_lecture_label_cleans_titles_and_truncates():
    lecture = SimpleNamespace(
        id=1,
        title="Lecture 4: Cranial Nerves",
        order=4,
        is_premium=True,
    )

    label = lecture_label(
        lecture, teacher_name="Dr A Sharma", watched=True
    )

    assert label == "✅ 💎 L4 · Cranial Nerves · Dr A Sharma"
    long_label = lecture_label(
        SimpleNamespace(
            id=2,
            title="A very long lecture title " * 5,
            order=1,
            is_premium=False,
        )
    )
    assert len(long_label) <= 60
    assert long_label.endswith("…")


def test_resolve_lecture_title_prefers_specific_available_text():
    assert resolve_lecture_title(
        "Anatomy",
        subject_name="Anatomy",
        topic_names=("Skull",),
    ) == "Skull"
    assert resolve_lecture_title(
        "Anatomy L3",
        subject_name="Anatomy",
        topic_names=("Skull",),
    ) == "Skull"
    assert resolve_lecture_title(
        "Anatomy",
        subject_name="Anatomy",
        raw_caption="Anatomy | Skull | Lecture 3 | Dr A",
    ) == "Skull"
    assert resolve_lecture_title(
        "Anatomy",
        subject_name="Anatomy",
        raw_caption="Anatomy / Dr A / Neuroanatomy / Brainstem",
    ) == "Brainstem"
    assert resolve_lecture_title(
        "Anatomy",
        subject_name="Anatomy",
        topic_names=("Skull",),
        raw_caption="Anatomy / Dr A / Skull / Brainstem",
    ) == "Brainstem"
    assert resolve_lecture_title(
        "Anatomy",
        subject_name="Anatomy",
        raw_caption="Anatomy / Skull / Lecture 3 / Dr A",
    ) == "Skull"
    assert resolve_lecture_title(
        "Anatomy", subject_name="Anatomy"
    ) == ""


async def test_subjects_show_lecture_counts(session, ux_catalogue):
    subject, user = ux_catalogue
    await _lectures(session, subject.id, ["Orientation", "Cells"])
    message = _message(user.id)

    await show_subjects(message, session)

    labels = _button_labels(message.answer.await_args.kwargs["reply_markup"])
    assert "🫀 UX Anatomy · 2" in labels


async def test_subject_without_topics_opens_teacher_view(
    session, ux_catalogue
):
    subject, user = ux_catalogue
    teacher, error = await add_teacher(session, subject.id, "Dr UX")
    assert error is None
    await _lectures(session, subject.id, ["Orientation"], teacher.id)

    cb = _callback(f"subj:{subject.slug}")
    await open_subject(cb, session, user)

    data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    assert f"tlec:{subject.id}:{teacher.id}:0" in data
    assert not any(value.startswith("topics:") for value in data)


async def test_teacher_lectures_number_and_mark_only_watched(
    session, ux_catalogue
):
    subject, user = ux_catalogue
    teacher, error = await add_teacher(session, subject.id, "Dr UX")
    assert error is None
    orientation, cells = await _lectures(
        session, subject.id, ["Orientation", "Cells"], teacher.id
    )
    await mark_watched(session, user.id, orientation.id)

    cb = _callback(f"tlec:{subject.id}:{teacher.id}:0")
    await open_teacher_lectures(cb, session, user)

    labels = _button_labels(
        cb.message.edit_text.await_args.kwargs["reply_markup"]
    )
    assert "✅ L1 · Orientation" in labels
    assert "L2 · Cells" in labels
    assert f"lec:{cells.id}:tch:{subject.id}:{teacher.id}:0" in (
        _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    )


async def test_bookmarks_paginate_and_preserve_back_context(
    session, ux_catalogue
):
    subject, user = ux_catalogue
    lectures = await _lectures(
        session, subject.id, [f"Saved topic {index}" for index in range(10)]
    )
    for lecture in lectures:
        await add_bookmark(session, user.id, lecture.id)

    message = _message(user.id)
    await show_bookmarks(message, session, user, page=1)

    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    data = _button_data(markup)
    assert "Page 2 of 2" in text
    assert len([value for value in data if value.startswith("lec:")]) == 2
    assert "bookmarks:0" in data

    bookmark_callback = next(
        value
        for value in data
        if value.startswith("lec:") and value.endswith(":b:1")
    )
    cb = _callback(bookmark_callback)
    await open_lecture(cb, session, user, AsyncMock())
    back_data = _button_data(
        cb.message.edit_text.await_args.kwargs["reply_markup"]
    )
    assert "bookmarks:1" in back_data


async def test_bookmarks_empty_state_links_to_subjects(
    session, ux_catalogue
):
    _subject, user = ux_catalogue
    message = _message(user.id)

    await show_bookmarks(message, session, user)

    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "No bookmarks yet" in text
    assert _button_data(markup) == ["subjects"]


async def test_search_paginates_and_returns_to_results(
    session, ux_catalogue
):
    subject, user = ux_catalogue
    await _lectures(
        session, subject.id, [f"Enzyme topic {index}" for index in range(10)]
    )

    message = _message(user.id, "/search enzyme")
    await search_cmd(message, session, user)

    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    data = _button_data(markup)
    assert "10 lectures found" in text
    assert "Page 1 of 2" in text
    assert "searchpage:1" in data

    page_cb = _callback("searchpage:1")
    await search_page_callback(page_cb, session, user)
    page_text = page_cb.message.edit_text.await_args.args[0]
    page_data = _button_data(
        page_cb.message.edit_text.await_args.kwargs["reply_markup"]
    )
    assert "Page 2 of 2" in page_text
    result_callback = next(
        value for value in page_data if value.startswith("lec:")
    )

    lecture_cb = _callback(result_callback)
    await open_lecture(lecture_cb, session, user, AsyncMock())
    back_data = _button_data(
        lecture_cb.message.edit_text.await_args.kwargs["reply_markup"]
    )
    assert "back_search" in back_data

    back_cb = _callback("back_search")
    await back_search(back_cb, session, user)
    assert "Page 2 of 2" in back_cb.message.edit_text.await_args.args[0]


async def test_progress_empty_state_links_to_subjects(session, ux_catalogue):
    _subject, user = ux_catalogue
    message = _message(user.id)

    await progress_entry(message, session, user)

    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "No published lectures yet" in text
    assert _button_data(markup) == ["subjects", "reminder:toggle"]


async def test_continue_empty_state_links_to_subjects(session, ux_catalogue):
    _subject, user = ux_catalogue
    message = _message(user.id)

    await continue_learning(message, session, user)

    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "No lectures started yet" in text
    assert _button_data(markup) == ["subjects"]


async def test_continue_resumes_last_watched_lecture(
    session, ux_catalogue
):
    subject, user = ux_catalogue
    lecture = (await _lectures(session, subject.id, ["Heart"]))[0]
    await mark_watched(session, user.id, lecture.id)
    message = _message(user.id)

    await continue_learning(message, session, user)

    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "Heart" in text
    assert f"lec:{lecture.id}" in _button_data(markup)


def test_access_denial_is_specific_to_request_state():
    pending_text, pending = _access_denial(
        SimpleNamespace(
            access_status=PENDING,
            access_requested_at=datetime.utcnow(),
        )
    )
    rejected_text, rejected_pending = _access_denial(
        SimpleNamespace(
            access_status=REJECTED,
            access_requested_at=datetime.utcnow(),
        )
    )

    assert "awaiting admin review" in pending_text
    assert pending is True
    assert "was not approved" in rejected_text
    assert rejected_pending is False
