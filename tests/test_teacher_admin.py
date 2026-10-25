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


async def _approved_item(
    session, course, *, with_lecture=False, message_id=555
):
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
        source_message_id=message_id,
        media_type="video",
        status="classified",
        course_id=course.id,
        file_id="teacher-file",
        file_unique_id=f"teacher-admin-unique-{message_id}",
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
        (
            review.setteacherbulk_cmd,
            f"/setteacherbulk {item.id}|{teacher.id}",
        ),
    ]
    for handler, text in commands:
        message = _message(999, text)
        await handler(message, session)
        message.answer.assert_not_awaited()

    await session.refresh(item)
    assert item.teacher_id is None


async def test_setteacherbulk_assigns_many_items_and_linked_lecture(session):
    _subject, course = await _biochemistry(session)
    teacher, error = await add_teacher(session, course.subject_id, "Dr Bulk")
    assert error is None
    _batch, first, _lecture = await _approved_item(
        session, course, message_id=601
    )
    _batch, second, _lecture = await _approved_item(
        session, course, message_id=602
    )
    _batch, third, lecture = await _approved_item(
        session, course, with_lecture=True, message_id=603
    )

    message = _message(
        123,
        f"/setteacherbulk {third.id},{first.id},{second.id}|{teacher.id}",
    )
    await review.setteacherbulk_cmd(message, session)

    for item in (first, second, third):
        await session.refresh(item)
        assert item.teacher_id == teacher.id
        assert item.teacher_source == "manual"
    await session.refresh(lecture)
    assert lecture.teacher_id == teacher.id

    text = message.answer.await_args.args[0]
    assert f"Assigned teacher #{teacher.id} Dr Bulk to 3 items." in text
    assert f"Items: {third.id}, {first.id}, {second.id}" in text


async def test_setteacherbulk_rejects_invalid_duplicate_and_malformed_ids(
    session,
):
    _subject, course = await _biochemistry(session)
    teacher, error = await add_teacher(session, course.subject_id, "Dr Bulk")
    assert error is None
    _batch, item, _lecture = await _approved_item(session, course)

    for value in ("0", "-1", "1.5", "abc", "١", "2147483648"):
        message = _message(
            123, f"/setteacherbulk {item.id},{value}|{teacher.id}"
        )
        await review.setteacherbulk_cmd(message, session)
        assert "Invalid item IDs" in message.answer.await_args.args[0]

    duplicate = _message(
        123, f"/setteacherbulk {item.id},{item.id}|{teacher.id}"
    )
    await review.setteacherbulk_cmd(duplicate, session)
    assert "Duplicate item IDs" in duplicate.answer.await_args.args[0]

    for text in (
        f"/setteacherbulk {item.id} {teacher.id}",
        f"/setteacherbulk {item.id}|{teacher.id}|extra",
    ):
        malformed = _message(123, text)
        await review.setteacherbulk_cmd(malformed, session)
        assert "Usage: /setteacherbulk" in malformed.answer.await_args.args[0]

    await session.refresh(item)
    assert item.teacher_id is None


async def test_setteacherbulk_rejects_invalid_or_missing_teacher(session):
    _subject, course = await _biochemistry(session)
    teacher, error = await add_teacher(session, course.subject_id, "Dr Bulk")
    assert error is None
    _batch, item, _lecture = await _approved_item(session, course)

    for value in ("none", "0", "-", "abc", "١", "2147483648"):
        message = _message(123, f"/setteacherbulk {item.id}|{value}")
        await review.setteacherbulk_cmd(message, session)
        assert "Teacher ID must be a positive number" in (
            message.answer.await_args.args[0]
        )

    missing = _message(123, f"/setteacherbulk {item.id}|999999")
    await review.setteacherbulk_cmd(missing, session)
    text = missing.answer.await_args.args[0]
    assert "No changes were applied." in text
    assert "Teacher #999999 not found." in text

    await session.refresh(item)
    assert item.teacher_id is None


async def test_setteacherbulk_rejects_missing_items_without_partial_changes(
    session,
):
    _subject, course = await _biochemistry(session)
    teacher, error = await add_teacher(session, course.subject_id, "Dr Bulk")
    assert error is None
    _batch, item, _lecture = await _approved_item(session, course)

    message = _message(
        123, f"/setteacherbulk {item.id},999999|{teacher.id}"
    )
    await review.setteacherbulk_cmd(message, session)

    text = message.answer.await_args.args[0]
    assert "No changes were applied." in text
    assert "#999999: Item not found." in text
    await session.refresh(item)
    assert item.teacher_id is None


async def test_setteacherbulk_rejects_subject_mismatch_without_changes(session):
    _subject, course = await _biochemistry(session)
    teacher, error = await add_teacher(session, course.subject_id, "Dr Bulk")
    assert error is None
    _batch, first, _lecture = await _approved_item(session, course)

    other_subject = Subject(
        slug="physiology", name="Physiology", emoji="🫀", order=4
    )
    session.add(other_subject)
    await session.commit()
    await session.refresh(other_subject)
    other_course = Course(
        year="",
        subject_id=other_subject.id,
        name="Physiology",
        aliases="physiology",
    )
    session.add(other_course)
    await session.commit()
    await session.refresh(other_course)
    _batch, second, _lecture = await _approved_item(
        session, other_course, message_id=604
    )

    message = _message(
        123, f"/setteacherbulk {first.id},{second.id}|{teacher.id}"
    )
    await review.setteacherbulk_cmd(message, session)

    text = message.answer.await_args.args[0]
    assert "No changes were applied." in text
    assert f"#{second.id}: Teacher belongs to a different subject." in text
    for item in (first, second):
        await session.refresh(item)
        assert item.teacher_id is None


async def test_setteacherbulk_rolls_back_if_commit_fails(session, monkeypatch):
    _subject, course = await _biochemistry(session)
    old_teacher, error = await add_teacher(
        session, course.subject_id, "Dr Old"
    )
    assert error is None
    new_teacher, error = await add_teacher(
        session, course.subject_id, "Dr New"
    )
    assert error is None
    old_teacher_id = old_teacher.id
    new_teacher_id = new_teacher.id
    _batch, item, lecture = await _approved_item(
        session, course, with_lecture=True
    )
    item.teacher_id = old_teacher_id
    lecture.teacher_id = old_teacher_id
    await session.commit()

    async def fail_commit():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", fail_commit)
    message = _message(123, f"/setteacherbulk {item.id}|{new_teacher_id}")
    await review.setteacherbulk_cmd(message, session)

    assert "No changes were applied." in message.answer.await_args.args[0]
    await session.refresh(item)
    await session.refresh(lecture)
    assert item.teacher_id == old_teacher_id
    assert lecture.teacher_id == old_teacher_id


async def test_setteacherbulk_rejects_oversized_request(session):
    message = _message(
        123,
        "/setteacherbulk " + ",".join(str(i) for i in range(1, 202)) + "|1",
    )
    await review.setteacherbulk_cmd(message, session)
    assert "maximum is 200" in message.answer.await_args.args[0]
