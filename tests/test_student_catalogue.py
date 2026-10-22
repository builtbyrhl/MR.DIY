from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from sqlalchemy import func, select

from bot.db.ingestion_repo import approve_batch
from bot.db.models import Course, Lecture, Progress, Subject, UploadItem, User
from bot.db.repo import (
    add_bookmark,
    list_bookmarks,
    list_lectures,
    search_lectures,
    subject_progress,
)
from bot.handlers.lectures import watch_lecture
from bot.handlers.subjects import open_subject
from bot.services.access import APPROVED
from bot.services.catalog import (
    project_upload_to_lecture,
    sync_published_uploads_to_catalog,
)
from bot.services.ingestion import ingest_media
from bot.services.publishing import publish_item


@pytest_asyncio.fixture
async def biochemistry(session):
    subject = Subject(slug="biochemistry", name="Biochemistry", emoji="🧪", order=3)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)

    course = Course(
        year="",
        subject_id=subject.id,
        name="Biochemistry",
        aliases="biochemistry,biochem",
    )
    session.add(course)
    await session.commit()
    await session.refresh(course)
    return subject, course


async def _student(session, user_id=9001):
    user = User(
        id=user_id,
        username="student",
        full_name="Student",
        is_premium=False,
        is_admin=False,
        access_status=APPROVED,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _ingest(
    session,
    *,
    message_id,
    caption,
    file_id="biochemistry-file-id",
    file_name=None,
    media_type="video",
):
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=message_id,
        media_type=media_type,
        caption=caption,
        file_name=file_name,
        file_id=file_id,
        file_unique_id=f"unique-{message_id}",
    )
    return item


async def _publish(session, course, item):
    course.destination_chat_id = "-100123"
    course.topic_id = 8
    await session.commit()
    ok, text = await approve_batch(session, item.batch_id, 123)
    assert ok is True, text

    bot = AsyncMock()
    bot.send_video.return_value = SimpleNamespace(message_id=700 + item.id)
    bot.send_document.return_value = SimpleNamespace(message_id=800 + item.id)
    result = await publish_item(session, bot, item.id)
    assert result.status == "published"
    await session.refresh(item)
    return bot


def _callback(data, user_id=9001):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def _button_data(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


async def test_publish_projects_upload_into_linked_lecture(
    session, biochemistry
):
    subject, course = biochemistry
    item = await _ingest(
        session,
        message_id=101,
        caption="Biochemistry Lecture 8 - Enzymes",
    )
    assert item.course_id == course.id

    await _publish(session, course, item)

    lecture = await session.scalar(select(Lecture))
    assert lecture is not None
    assert lecture.source_upload_item_id == item.id
    assert lecture.subject_id == subject.id
    assert lecture.title == "Biochemistry Lecture 8 - Enzymes"
    assert lecture.order == 8
    assert lecture.channel_message_id is None


async def test_publish_and_sync_are_idempotent(session, biochemistry):
    _, course = biochemistry
    item = await _ingest(
        session,
        message_id=102,
        caption="Biochemistry Lecture 2 - Carbohydrates",
    )
    await _publish(session, course, item)

    lecture, created = await project_upload_to_lecture(session, item)
    await session.commit()
    assert created is False
    assert lecture is not None

    created_count = await sync_published_uploads_to_catalog(session)
    assert created_count == 0
    count = await session.scalar(select(func.count(Lecture.id)))
    assert count == 1


async def test_sync_backfills_existing_published_upload(session, biochemistry):
    _, course = biochemistry
    item = await _ingest(
        session,
        message_id=103,
        caption="Biochemistry Lecture 4 - Lipids",
        file_id="existing-published-file",
    )
    item.publication_status = "published"
    item.published_message_id = 333
    await session.commit()

    first = await sync_published_uploads_to_catalog(session)
    second = await sync_published_uploads_to_catalog(session)

    assert first == 1
    assert second == 0
    lecture = await session.scalar(select(Lecture))
    assert lecture is not None
    assert lecture.source_upload_item_id == item.id
    assert lecture.title == "Biochemistry Lecture 4 - Lipids"


async def test_subject_callback_lists_projected_uploads_in_order(
    session, biochemistry
):
    subject, course = biochemistry
    user = await _student(session)
    later = await _ingest(
        session,
        message_id=104,
        caption="Biochemistry Lecture 8 - Enzymes",
    )
    earlier = await _ingest(
        session,
        message_id=105,
        caption="Biochemistry Lecture 3 - Proteins",
        file_id="earlier-file-id",
    )
    for item in (later, earlier):
        item.publication_status = "published"
        item.published_message_id = 400 + item.id
    await session.commit()
    await sync_published_uploads_to_catalog(session)

    cb = _callback("subj:biochemistry", user.id)
    await open_subject(cb, session, user)

    cb.message.edit_text.assert_awaited_once()
    text = cb.message.edit_text.await_args.args[0]
    assert "Biochemistry" in text
    assert "Progress: 0/2" in text

    data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    lecture_ids = [value for value in data if value.startswith("lec:")]
    lectures = (
        await session.scalars(
            select(Lecture).where(Lecture.subject_id == subject.id).order_by(Lecture.order)
        )
    ).all()
    assert lecture_ids == [f"lec:{lecture.id}" for lecture in lectures]
    assert lecture_ids[0] == f"lec:{earlier.id}"
    assert lecture_ids[1] == f"lec:{later.id}"
    cb.answer.assert_awaited_once_with()


async def test_empty_subject_callback_explains_missing_lectures(
    session, biochemistry
):
    _, _ = biochemistry
    user = await _student(session)

    cb = _callback("subj:biochemistry", user.id)
    await open_subject(cb, session, user)

    text = cb.message.edit_text.await_args.args[0]
    assert "No lectures uploaded yet for this subject." in text
    data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    assert data == ["subjects"]
    cb.answer.assert_awaited_once_with()


async def test_watch_sends_projected_video_file_id_and_marks_progress(
    session, biochemistry
):
    _, course = biochemistry
    user = await _student(session)
    item = await _ingest(
        session,
        message_id=106,
        caption="Biochemistry Lecture 5 - Vitamins",
        file_id="projected-video-file",
    )
    bot = await _publish(session, course, item)
    lecture = await session.scalar(select(Lecture))
    assert lecture is not None

    bot.reset_mock()
    cb = _callback(f"watch:{lecture.id}", user.id)
    await watch_lecture(cb, session, user, bot)

    bot.send_video.assert_awaited_once()
    kwargs = bot.send_video.await_args.kwargs
    assert kwargs["chat_id"] == user.id
    assert kwargs["video"] == "projected-video-file"
    assert kwargs["caption"] == "Biochemistry Lecture 5 - Vitamins"
    assert kwargs["protect_content"] is True
    bot.copy_message.assert_not_awaited()

    watched = await session.scalar(
        select(Progress).where(
            Progress.user_id == user.id,
            Progress.lecture_id == lecture.id,
            Progress.watched.is_(True),
        )
    )
    assert watched is not None
    cb.answer.assert_awaited_once_with("Sent! Marked as watched ✅")


async def test_watch_sends_projected_document_file_id(session, biochemistry):
    _, course = biochemistry
    user = await _student(session)
    item = await _ingest(
        session,
        message_id=107,
        caption="Biochemistry Lecture 6 - Notes",
        file_name="Biochemistry Lecture 6.pdf",
        file_id="projected-document-file",
        media_type="document",
    )
    bot = await _publish(session, course, item)
    lecture = await session.scalar(select(Lecture))
    assert lecture is not None

    bot.reset_mock()
    cb = _callback(f"watch:{lecture.id}", user.id)
    await watch_lecture(cb, session, user, bot)

    bot.send_document.assert_awaited_once()
    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == user.id
    assert kwargs["document"] == "projected-document-file"
    assert kwargs["caption"] == "Biochemistry Lecture 6 - Notes"
    assert kwargs["protect_content"] is True
    bot.send_video.assert_not_awaited()


async def test_watch_keeps_legacy_channel_copy_path(
    session, biochemistry, monkeypatch
):
    subject, _ = biochemistry
    user = await _student(session)
    lecture = Lecture(
        subject_id=subject.id,
        title="Legacy Lecture",
        channel_message_id=321,
        order=1,
    )
    session.add(lecture)
    await session.commit()
    await session.refresh(lecture)

    monkeypatch.setattr(
        "bot.handlers.lectures.get_settings",
        lambda: SimpleNamespace(channel_id="-100777"),
    )

    bot = AsyncMock()
    cb = _callback(f"watch:{lecture.id}", user.id)
    await watch_lecture(cb, session, user, bot)

    bot.copy_message.assert_awaited_once_with(
        chat_id=user.id,
        from_chat_id="-100777",
        message_id=321,
        protect_content=True,
    )
    bot.send_video.assert_not_awaited()


async def test_search_bookmarks_and_progress_include_projected_lecture(
    session, biochemistry
):
    subject, course = biochemistry
    user = await _student(session)
    item = await _ingest(
        session,
        message_id=108,
        caption="Biochemistry Lecture 7 - Molecular Biology",
    )
    await _publish(session, course, item)
    lecture = await session.scalar(select(Lecture))
    assert lecture is not None

    results = await search_lectures(session, "molecular")
    assert [result.id for result in results] == [lecture.id]

    assert await add_bookmark(session, user.id, lecture.id) is True
    bookmarks = await list_bookmarks(session, user.id)
    assert [bookmark.id for bookmark in bookmarks] == [lecture.id]

    done, total = await subject_progress(session, user.id, subject.id)
    assert (done, total) == (0, 1)
    await session.refresh(lecture)
    assert lecture in await list_lectures(session, subject.id, include_premium=True)


async def test_watch_rejects_premium_lecture_for_free_user(
    session, biochemistry
):
    subject, _ = biochemistry
    user = await _student(session)
    lecture = Lecture(
        subject_id=subject.id,
        title="Premium Lecture",
        channel_message_id=654,
        is_premium=True,
        order=1,
    )
    session.add(lecture)
    await session.commit()
    await session.refresh(lecture)

    bot = AsyncMock()
    cb = _callback(f"watch:{lecture.id}", user.id)
    await watch_lecture(cb, session, user, bot)

    bot.copy_message.assert_not_awaited()
    bot.send_video.assert_not_awaited()
    cb.answer.assert_awaited_once_with(
        "Premium lecture. Upgrade to watch.",
        show_alert=True,
    )
