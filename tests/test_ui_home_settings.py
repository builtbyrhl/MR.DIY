from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio

from bot.db.models import Lecture, Subject, UploadBatch, UploadItem, User
from bot.db.repo import mark_watched, next_lecture_in_subject
from bot.handlers.lectures import watch_lecture
from bot.handlers.start import (
    build_home,
    cb_home,
    cb_settings_access,
    settings_entry,
)
from bot.handlers.progress import cb_progress
from bot.services.access import APPROVED


@pytest_asyncio.fixture
async def ux(session):
    subject = Subject(slug="anatomy", name="Anatomy", emoji="🦴", order=1)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)

    batch = UploadBatch(source_chat_id=-100, batch_date=date.today())
    session.add(batch)
    await session.commit()
    await session.refresh(batch)

    upload = UploadItem(
        batch_id=batch.id,
        source_chat_id=-100,
        source_message_id=11,
        media_type="video",
        file_id="file-heart",
        status="ready",
        publication_status="published",
    )
    session.add(upload)
    await session.commit()
    await session.refresh(upload)

    heart = Lecture(
        subject_id=subject.id,
        title="Heart",
        order=1,
        source_upload_item_id=upload.id,
    )
    lungs = Lecture(subject_id=subject.id, title="Lungs", order=2)
    session.add_all([heart, lungs])
    await session.commit()
    for lecture in (heart, lungs):
        await session.refresh(lecture)

    user = User(
        id=9100,
        username="student",
        full_name="Student",
        is_premium=False,
        is_admin=False,
        access_status=APPROVED,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {
        "subject": subject,
        "heart": heart,
        "lungs": lungs,
        "user": user,
        "upload": upload,
    }


def _message(user_id=9100):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, full_name="Student"),
        chat=SimpleNamespace(type="private"),
        answer=AsyncMock(),
    )


def _callback(data, user_id=9100):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Student"),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )


def _buttons(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


async def test_home_offers_first_lecture_for_new_student(session, ux):
    text, kb = await build_home(session, ux["user"])
    assert "Start here" in text
    assert "Heart" in text
    assert f"lec:{ux['heart'].id}" in _buttons(kb)


async def test_home_resumes_next_unwatched_lecture(session, ux):
    await mark_watched(session, ux["user"].id, ux["heart"].id)
    text, kb = await build_home(session, ux["user"])
    assert "Continue where you left off" in text
    assert f"lec:{ux['lungs'].id}" in _buttons(kb)


async def test_home_falls_back_to_last_watched_lecture(session, ux):
    user = ux["user"]
    await mark_watched(session, user.id, ux["heart"].id)
    await mark_watched(session, user.id, ux["lungs"].id)
    text, kb = await build_home(session, user)
    assert "Continue where you left off" in text
    assert f"lec:{ux['lungs'].id}" in _buttons(kb)


async def test_home_callback_edits_message(session, ux):
    cb = _callback("home")
    await cb_home(cb, session, ux["user"])
    text = cb.message.edit_text.await_args.args[0]
    assert "FMGE Lecture Bot" in text
    cb.answer.assert_awaited_once_with()


async def test_settings_screen_lists_shortcuts(session, ux):
    message = _message()
    await settings_entry(message, ux["user"])
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert {
        "settings:access",
        "settings:search",
        "institutions",
        "settings:bookmarks",
        "settings:help",
        "home",
    }.issubset(set(_buttons(markup)))


async def test_settings_access_renders_access_view(session, ux):
    cb = _callback("settings:access")
    await cb_settings_access(cb, ux["user"])
    text = cb.message.edit_text.await_args.args[0]
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert "My Access" in text
    assert "settings:menu" in _buttons(markup)


async def test_progress_callback_edits_message(session, ux):
    cb = _callback("progress")
    await cb_progress(cb, session, ux["user"])
    text = cb.message.edit_text.await_args.args[0]
    assert "Your progress" in text


async def test_next_lecture_helper_returns_following_order(session, ux):
    nxt = await next_lecture_in_subject(session, ux["heart"])
    assert nxt is not None
    assert nxt.id == ux["lungs"].id
    assert await next_lecture_in_subject(session, ux["lungs"]) is None


async def test_watch_marks_done_and_offers_next(session, ux):
    bot = AsyncMock()
    bot.send_video.return_value = SimpleNamespace(message_id=777)
    cb = _callback(f"watch:{ux['heart'].id}")

    await watch_lecture(cb, session, ux["user"], bot)

    text = cb.message.edit_text.await_args.args[0]
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert "Watched" in text
    assert f"lec:{ux['lungs'].id}" in _buttons(markup)
    assert "home" in _buttons(markup)
