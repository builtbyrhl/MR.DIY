from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from bot.db.models import Course, Lecture, Subject, Teacher, UploadBatch, UploadItem
from bot.db.repo import add_teacher
from bot.handlers import review


def _message(user_id, text):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


async def _biochemistry(session):
    subject = Subject(slug="biochemistry", name="Biochemistry", emoji="🧪", order=3)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)

    course = Course(
        year="",
        subject_id=subject.id,
        name="Biochemistry",
        aliases="biochemistry",
    )
    session.add(course)
    await session.commit()
    await session.refresh(course)
    return subject, course


async def _approved_item(session, course, *, with_lecture=False):
    batch = UploadBatch(
        source_chat_id=-1001,
        batch_date=date(2026, 1, 15),
        status="approved",
    )
    session.add(batch)
    await session.commit()
    await session.refresh(batch)

    item = UploadItem(
        batch_id=batch.id,
        source_chat_id=-1001,
        source_message_id=555,
        media_type="video",
        status="classified",
        course_id=course.id,
        file_id="teacher-file",
        file_unique_id="teacher-admin-unique",
        lecture_number=1,
        title="Biochemistry Lecture 1",
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)

    lecture = None
    if with_lecture:
        lecture = Lecture(
            subject_id=course.subject_id,
            source_upload_item_id=item.id,
            title="Biochemistry Lecture 1",
            order=1,
        )
        session.add(lecture)
        await session.commit()
        await session.refresh(lecture)
    return batch, item, lecture


async def test_addteacher_and_teachers_command(session):
    subject, _course = await _biochemistry(session)

    added = _message(123, "/addteacher biochemistry|Dr. A. Sharma")
    await review.addteacher_cmd(added, session)
    text = added.answer.await_args.args[0]
    assert "Added teacher" in text
    assert "Biochemistry" in text

    duplicate = _message(123, "/addteacher biochemistry|Dr A Sharma")
    await review.addteacher_cmd(duplicate, session)
    assert "already exists" in duplicate.answer.await_args.args[0]

    missing_subject = _message(123, "/addteacher nope|Dr A")
    await review.addteacher_cmd(missing_subject, session)
    assert "not found" in missing_subject.answer.await_args.args[0]

    teacher = await session.scalar(
        select(Teacher).where(Teacher.subject_id == subject.id)
    )
    assert teacher is not None

    listing = _message(123, "/teachers biochemistry")
    await review.teachers_cmd(listing, session)
    listing_text = "\n".join(
        call.args[0] for call in listing.answer.await_args_list
    )
    assert f"#{teacher.id}" in listing_text
    assert "Dr. A. Sharma" in listing_text


async def test_setteacher_updates_item_and_linked_lecture_after_approval(
    session
):
    _subject, course = await _biochemistry(session)
    teacher, error = await add_teacher(session, course.subject_id, "Dr A")
    assert error is None
    _batch, item, lecture = await _approved_item(
        session, course, with_lecture=True
    )

    assigned = _message(123, f"/setteacher {item.id}|{teacher.id}")
    await review.setteacher_cmd(assigned, session)

    await session.refresh(item)
    await session.refresh(lecture)
    assert item.teacher_id == teacher.id
    assert lecture.teacher_id == teacher.id
    assert "Teacher set" in assigned.answer.await_args.args[0]

    cleared = _message(123, f"/setteacher {item.id}|none")
    await review.setteacher_cmd(cleared, session)
    await session.refresh(item)
    await session.refresh(lecture)
    assert item.teacher_id is None
    assert lecture.teacher_id is None
    assert "cleared" in cleared.answer.await_args.args[0]


async def test_setlectureteacher_updates_item_for_future_projection(
    session
):
    _subject, course = await _biochemistry(session)
    teacher, error = await add_teacher(session, course.subject_id, "Dr B")
    assert error is None
    _batch, item, lecture = await _approved_item(
        session, course, with_lecture=True
    )

    message = _message(123, f"/setlectureteacher {lecture.id}|{teacher.id}")
    await review.setlectureteacher_cmd(message, session)

    await session.refresh(item)
    await session.refresh(lecture)
    assert lecture.teacher_id == teacher.id
    assert item.teacher_id == teacher.id
    assert f"#{teacher.id}" in message.answer.await_args.args[0]


async def test_setteacher_rejects_invalid_teacher_id(session):
    _subject, course = await _biochemistry(session)
    _batch, item, _lecture = await _approved_item(session, course)

    for value in ("abc", "-1", "0.5"):
        message = _message(123, f"/setteacher {item.id}|{value}")
        await review.setteacher_cmd(message, session)
        assert "Teacher ID" in message.answer.await_args.args[0]

    await session.refresh(item)
    assert item.teacher_id is None


async def test_batch_preview_shows_assigned_teacher(session):
    _subject, course = await _biochemistry(session)
    teacher, error = await add_teacher(session, course.subject_id, "Dr Preview")
    assert error is None
    batch, item, _lecture = await _approved_item(session, course)
    item.teacher_id = teacher.id
    await session.commit()

    message = _message(123, f"/batch {batch.id}")
    await review.batch_cmd(message, session)

    text = "\n".join(call.args[0] for call in message.answer.await_args_list)
    assert f"teacher: #{teacher.id} Dr Preview" in text


async def test_teacher_commands_are_admin_only(session):
    _subject, course = await _biochemistry(session)
    teacher, error = await add_teacher(session, course.subject_id, "Dr A")
    assert error is None
    _batch, item, lecture = await _approved_item(
        session, course, with_lecture=True
    )

    commands = [
        (review.addteacher_cmd, "/addteacher biochemistry|Dr X"),
        (review.teachers_cmd, "/teachers biochemistry"),
        (review.setteacher_cmd, f"/setteacher {item.id}|{teacher.id}"),
        (
            review.setlectureteacher_cmd,
            f"/setlectureteacher {lecture.id}|{teacher.id}",
        ),
    ]
    for handler, text in commands:
        message = _message(999, text)
        await handler(message, session)
        message.answer.assert_not_awaited()
