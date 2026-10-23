from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from sqlalchemy import select

from bot.db.ingestion_repo import (
    approve_batch,
    set_item_course,
    set_item_teacher,
)
from bot.db.models import Course, Lecture, Subject, Teacher, User
from bot.db.repo import (
    TEACHER_UNSPECIFIED,
    add_teacher,
    list_lectures_by_teacher,
    set_lecture_teacher,
)
from bot.handlers.lectures import open_lecture
from bot.handlers.subjects import (
    back_teacher,
    lec_page,
    open_subject,
    open_teacher_lectures,
    teacher_page,
)
from bot.services.access import APPROVED
from bot.services.catalog import sync_published_uploads_to_catalog
from bot.services.ingestion import ingest_media, retry_item
from bot.services.publishing import publish_item


@pytest_asyncio.fixture
async def catalogue(session):
    biochemistry = Subject(
        slug="biochemistry", name="Biochemistry", emoji="🧪", order=3
    )
    anatomy = Subject(slug="anatomy", name="Anatomy", emoji="🫀", order=1)
    session.add_all([biochemistry, anatomy])
    await session.commit()
    await session.refresh(biochemistry)
    await session.refresh(anatomy)

    course = Course(
        year="",
        subject_id=biochemistry.id,
        name="Biochemistry",
        aliases="biochemistry",
    )
    session.add(course)
    await session.commit()
    await session.refresh(course)

    user = User(
        id=9001,
        username="student",
        full_name="Student",
        is_premium=False,
        is_admin=False,
        access_status=APPROVED,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return biochemistry, anatomy, course, user


async def _teacher(session, subject_id, name):
    teacher, error = await add_teacher(session, subject_id, name)
    assert error is None
    return teacher


def _lecture(subject_id, title, order, teacher_id=None, premium=False):
    return Lecture(
        subject_id=subject_id,
        title=title,
        order=order,
        teacher_id=teacher_id,
        is_premium=premium,
    )


async def _add_lectures(session, lectures):
    session.add_all(lectures)
    await session.commit()
    for lecture in lectures:
        await session.refresh(lecture)
    return lectures


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


def _button_labels(markup):
    return [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]


def _callback_lengths(markup):
    return {
        len(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


async def test_subject_shows_only_its_teachers_with_counts_and_unspecified(
    session, catalogue
):
    biochemistry, anatomy, _course, user = catalogue
    sharma = await _teacher(session, biochemistry.id, "Dr. A Sharma")
    rao = await _teacher(session, biochemistry.id, "Dr B Rao")
    other = await _teacher(session, anatomy.id, "Dr Other Subject")
    await _add_lectures(
        session,
        [
            _lecture(biochemistry.id, "Lecture 1", 1, sharma.id),
            _lecture(biochemistry.id, "Lecture 2", 2, sharma.id),
            _lecture(biochemistry.id, "Lecture 3", 3, rao.id),
            _lecture(biochemistry.id, "Lecture 4", 4),
            _lecture(anatomy.id, "Anatomy 1", 1, other.id),
        ],
    )

    cb = _callback(f"subj:{biochemistry.slug}", user.id)
    await open_subject(cb, session, user)

    text = cb.message.edit_text.await_args.args[0]
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    data = _button_data(markup)
    labels = _button_labels(markup)

    assert "Progress: 0/4" in text
    assert "Choose a teacher:" in text
    assert f"tlec:{biochemistry.id}:{sharma.id}:0" in data
    assert f"tlec:{biochemistry.id}:{rao.id}:0" in data
    assert f"tlec:{biochemistry.id}:0:0" in data
    assert all(f":{other.id}:" not in value for value in data)
    assert "Dr. A Sharma (2)" in labels
    assert "Dr B Rao (1)" in labels
    assert f"{TEACHER_UNSPECIFIED} (1)" in labels
    assert max(_callback_lengths(markup)) <= 64
    cb.answer.assert_awaited_once_with()


async def test_teacher_selection_filters_and_orders_lectures(
    session, catalogue
):
    biochemistry, _anatomy, _course, user = catalogue
    sharma = await _teacher(session, biochemistry.id, "Dr. A Sharma")
    rao = await _teacher(session, biochemistry.id, "Dr B Rao")
    later, earlier, other, unspecified, premium = await _add_lectures(
        session,
        [
            _lecture(biochemistry.id, "Repeated Title", 8, sharma.id),
            _lecture(biochemistry.id, "Repeated Title", 3, sharma.id),
            _lecture(biochemistry.id, "Other Teacher", 4, rao.id),
            _lecture(biochemistry.id, "Unspecified", 5),
            _lecture(biochemistry.id, "Premium Lecture", 6, sharma.id, True),
        ],
    )

    cb = _callback(f"tlec:{biochemistry.id}:{sharma.id}:0", user.id)
    await open_teacher_lectures(cb, session, user)

    text = cb.message.edit_text.await_args.args[0]
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    data = _button_data(markup)
    lecture_ids = [value for value in data if value.startswith("lec:")]

    assert "A Sharma" in text
    assert f"lec:{earlier.id}" in lecture_ids
    assert f"lec:{later.id}" in lecture_ids
    assert f"lec:{premium.id}" in lecture_ids
    assert f"lec:{other.id}" not in lecture_ids
    assert f"lec:{unspecified.id}" not in lecture_ids
    assert lecture_ids.index(f"lec:{earlier.id}") < lecture_ids.index(
        f"lec:{later.id}"
    )
    assert "💎 Premium Lecture" in _button_labels(markup)
    assert f"teachers:{biochemistry.id}:0" in data
    assert max(_callback_lengths(markup)) <= 64
    cb.answer.assert_awaited_once_with()


async def test_unspecified_teacher_bucket_lists_only_unassigned_lectures(
    session, catalogue
):
    biochemistry, _anatomy, _course, user = catalogue
    teacher = await _teacher(session, biochemistry.id, "Dr A")
    assigned, unassigned = await _add_lectures(
        session,
        [
            _lecture(biochemistry.id, "Assigned", 1, teacher.id),
            _lecture(biochemistry.id, "Unassigned", 2),
        ],
    )

    cb = _callback(f"tlec:{biochemistry.id}:0:0", user.id)
    await open_teacher_lectures(cb, session, user)

    text = cb.message.edit_text.await_args.args[0]
    data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    lecture_ids = [value for value in data if value.startswith("lec:")]

    assert TEACHER_UNSPECIFIED in text
    assert f"lec:{unassigned.id}" in lecture_ids
    assert f"lec:{assigned.id}" not in lecture_ids


async def test_teacher_list_pagination_clamps_and_uses_short_callbacks(
    session, catalogue
):
    biochemistry, _anatomy, _course, user = catalogue
    teachers = []
    for index in range(10):
        teacher = await _teacher(session, biochemistry.id, f"Teacher {index:02d}")
        teachers.append(teacher)
    await _add_lectures(
        session,
        [
            _lecture(biochemistry.id, f"Lecture {index}", index + 1, teacher.id)
            for index, teacher in enumerate(teachers)
        ],
    )

    cb = _callback(f"teachers:{biochemistry.id}:99", user.id)
    await teacher_page(cb, session, user)

    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    data = _button_data(markup)
    teacher_callbacks = [value for value in data if value.startswith("tlec:")]
    assert teacher_callbacks == [
        f"tlec:{biochemistry.id}:{teachers[8].id}:0",
        f"tlec:{biochemistry.id}:{teachers[9].id}:0",
    ]
    assert f"teachers:{biochemistry.id}:0" in data
    assert f"teachers:{biochemistry.id}:2" not in data
    assert max(_callback_lengths(markup)) <= 64


async def test_teacher_lecture_pagination_and_back_callback(
    session, catalogue
):
    biochemistry, _anatomy, _course, user = catalogue
    teacher = await _teacher(session, biochemistry.id, "Dr A")
    lectures = await _add_lectures(
        session,
        [
            _lecture(biochemistry.id, f"Lecture {index}", index + 1, teacher.id)
            for index in range(10)
        ],
    )

    cb = _callback(f"tlec:{biochemistry.id}:{teacher.id}:1", user.id)
    await open_teacher_lectures(cb, session, user)
    data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    lecture_ids = [value for value in data if value.startswith("lec:")]
    assert lecture_ids == [
        f"lec:{lectures[8].id}",
        f"lec:{lectures[9].id}",
    ]
    assert f"tlec:{biochemistry.id}:{teacher.id}:0" in data
    assert f"teachers:{biochemistry.id}:0" in data

    back = _callback(f"back_teacher:{biochemistry.id}:{teacher.id}", user.id)
    await back_teacher(back, session, user)
    back_data = _button_data(
        back.message.edit_text.await_args.kwargs["reply_markup"]
    )
    assert [value for value in back_data if value.startswith("lec:")] == [
        f"lec:{lecture.id}" for lecture in lectures[:8]
    ]
    back.answer.assert_awaited_once_with()


async def test_lecture_back_button_opens_its_teacher_context(
    session, catalogue
):
    biochemistry, _anatomy, _course, user = catalogue
    teacher = await _teacher(session, biochemistry.id, "Dr A")
    lecture, = await _add_lectures(
        session, [_lecture(biochemistry.id, "Lecture 1", 1, teacher.id)]
    )

    cb = _callback(f"lec:{lecture.id}", user.id)
    await open_lecture(cb, session, user, AsyncMock())

    data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    assert f"back_teacher:{biochemistry.id}:{teacher.id}" in data

    back = _callback(f"back_teacher:{biochemistry.id}:{teacher.id}", user.id)
    await back_teacher(back, session, user)
    back_data = _button_data(
        back.message.edit_text.await_args.kwargs["reply_markup"]
    )
    assert f"lec:{lecture.id}" in back_data


async def test_legacy_lecture_page_callback_still_works(session, catalogue):
    biochemistry, _anatomy, _course, user = catalogue
    teacher = await _teacher(session, biochemistry.id, "Dr A")
    lecture, = await _add_lectures(
        session, [_lecture(biochemistry.id, "Legacy View", 1, teacher.id)]
    )

    cb = _callback(f"lecpage:{biochemistry.slug}:0", user.id)
    await lec_page(cb, session, user)

    data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    assert f"lec:{lecture.id}" in data
    assert "subjects" in data


async def test_teacher_projection_is_idempotent_and_preserves_corrections(
    session, catalogue
):
    biochemistry, _anatomy, course, _user = catalogue
    original = await _teacher(session, biochemistry.id, "Dr Original")
    corrected = await _teacher(session, biochemistry.id, "Dr Corrected")
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=501,
        media_type="video",
        caption="Biochemistry Lecture 1 - Cells",
        file_name=None,
        file_id="teacher-file",
        file_unique_id="teacher-unique",
    )
    item, error = await set_item_teacher(session, item.id, original.id)
    assert error is None
    item.publication_status = "published"
    item.published_message_id = 777
    await session.commit()

    await sync_published_uploads_to_catalog(session)
    lecture = await session.scalar(
        select(Lecture).where(Lecture.source_upload_item_id == item.id)
    )
    assert lecture.teacher_id == original.id

    lecture, error = await set_lecture_teacher(session, lecture.id, corrected.id)
    assert error is None
    assert lecture.teacher_id == corrected.id
    await session.refresh(item)
    assert item.teacher_id == corrected.id

    await sync_published_uploads_to_catalog(session)
    await session.refresh(lecture)
    await session.refresh(item)
    assert lecture.teacher_id == corrected.id
    assert item.teacher_id == corrected.id

    lecture, error = await set_lecture_teacher(session, lecture.id, None)
    assert error is None
    await session.refresh(item)
    assert lecture.teacher_id is None
    assert item.teacher_id is None


async def test_publish_projects_teacher_and_correction_survives_retry_sync(
    session, catalogue
):
    biochemistry, _anatomy, course, _user = catalogue
    original = await _teacher(session, biochemistry.id, "Dr Original")
    corrected = await _teacher(session, biochemistry.id, "Dr Corrected")
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=503,
        media_type="video",
        caption="Biochemistry Lecture 3 - Cells",
        file_name=None,
        file_id="publish-teacher-file",
        file_unique_id="publish-teacher-unique",
    )
    item, error = await set_item_teacher(session, item.id, original.id)
    assert error is None

    course.destination_chat_id = "-100123"
    course.topic_id = 8
    await session.commit()
    ok, text = await approve_batch(session, item.batch_id, 123)
    assert ok is True, text

    bot = AsyncMock()
    bot.send_video.return_value = SimpleNamespace(message_id=901)
    result = await publish_item(session, bot, item.id)
    assert result.status == "published"

    lecture = await session.scalar(
        select(Lecture).where(Lecture.source_upload_item_id == item.id)
    )
    assert lecture.teacher_id == original.id

    lecture, error = await set_lecture_teacher(session, lecture.id, corrected.id)
    assert error is None

    result = await publish_item(session, bot, item.id)
    assert result.status == "already_published"
    await session.refresh(lecture)
    assert lecture.teacher_id == corrected.id

    await sync_published_uploads_to_catalog(session)
    await session.refresh(lecture)
    await session.refresh(item)
    assert lecture.teacher_id == corrected.id
    assert item.teacher_id == corrected.id


async def test_retry_preserves_teacher_until_course_subject_changes(
    session, catalogue
):
    biochemistry, anatomy, _course, _user = catalogue
    anatomy_course = Course(
        year="",
        subject_id=anatomy.id,
        name="Anatomy",
        aliases="anatomy",
    )
    session.add(anatomy_course)
    await session.commit()
    await session.refresh(anatomy_course)

    teacher = await _teacher(session, biochemistry.id, "Dr Biochemistry")
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=504,
        media_type="video",
        caption="Biochemistry Lecture 4 - Tissues",
        file_name=None,
        file_id="retry-teacher-file",
        file_unique_id="retry-teacher-unique",
    )
    item, error = await set_item_teacher(session, item.id, teacher.id)
    assert error is None

    item = await retry_item(session, item.id)
    assert item.teacher_id == teacher.id
    assert item.course_id != anatomy_course.id

    item.raw_caption = "Anatomy Lecture 4 - Tissues"
    await session.commit()
    item = await retry_item(session, item.id)
    assert item.course_id == anatomy_course.id
    assert item.teacher_id is None


async def test_course_correction_clears_teacher_only_for_new_subject(
    session, catalogue
):
    biochemistry, anatomy, course, _user = catalogue
    anatomy_course = Course(
        year="",
        subject_id=anatomy.id,
        name="Anatomy",
        aliases="anatomy",
    )
    session.add(anatomy_course)
    await session.commit()
    await session.refresh(anatomy_course)

    teacher = await _teacher(session, biochemistry.id, "Dr Biochemistry")
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=505,
        media_type="video",
        caption="Biochemistry Lecture 5 - Membranes",
        file_name=None,
        file_id="course-teacher-file",
        file_unique_id="course-teacher-unique",
    )
    item, error = await set_item_teacher(session, item.id, teacher.id)
    assert error is None

    item, error = await set_item_course(session, item.id, course.id)
    assert error is None
    assert item.teacher_id == teacher.id

    item, error = await set_item_course(session, item.id, anatomy_course.id)
    assert error is None
    assert item.teacher_id is None


async def test_teacher_assignment_rejects_subject_mismatch(
    session, catalogue
):
    biochemistry, anatomy, course, _user = catalogue
    anatomy_teacher = await _teacher(session, anatomy.id, "Dr Anatomy")
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=502,
        media_type="video",
        caption="Biochemistry Lecture 2 - Tissues",
        file_name=None,
        file_id="mismatch-file",
        file_unique_id="mismatch-unique",
    )
    assert item.course_id == course.id

    _, error = await set_item_teacher(session, item.id, anatomy_teacher.id)
    assert error == "Teacher belongs to a different subject."

    lecture, = await _add_lectures(
        session, [_lecture(biochemistry.id, "Lecture 2", 2)]
    )
    _, error = await set_lecture_teacher(session, lecture.id, anatomy_teacher.id)
    assert error == "Teacher belongs to a different subject."
    await session.refresh(lecture)
    await session.refresh(item)
    assert lecture.teacher_id is None
    assert item.teacher_id is None


async def test_teacher_names_are_normalized_per_subject(session, catalogue):
    biochemistry, anatomy, _course, _user = catalogue
    first = await _teacher(session, biochemistry.id, "Dr. A. Sharma")
    duplicate, error = await add_teacher(session, biochemistry.id, "Dr A Sharma")
    assert duplicate is None
    assert "already exists" in error

    same_name_other_subject = await _teacher(session, anatomy.id, "Dr A Sharma")
    assert same_name_other_subject.id != first.id

    teachers = (
        await session.scalars(
            select(Teacher).where(Teacher.subject_id == biochemistry.id)
        )
    ).all()
    assert [teacher.id for teacher in teachers] == [first.id]


async def test_teacher_filtered_lecture_query_supports_premium_filter(
    session, catalogue
):
    biochemistry, _anatomy, _course, _user = catalogue
    teacher = await _teacher(session, biochemistry.id, "Dr A")
    free, premium = await _add_lectures(
        session,
        [
            _lecture(biochemistry.id, "Free", 1, teacher.id),
            _lecture(biochemistry.id, "Premium", 2, teacher.id, True),
        ],
    )

    all_lectures = await list_lectures_by_teacher(
        session, biochemistry.id, teacher.id, include_premium=True
    )
    free_lectures = await list_lectures_by_teacher(
        session, biochemistry.id, teacher.id, include_premium=False
    )
    assert [lecture.id for lecture in all_lectures] == [free.id, premium.id]
    assert [lecture.id for lecture in free_lectures] == [free.id]
