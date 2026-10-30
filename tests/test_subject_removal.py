from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from bot.db.models import (
    AdminAction,
    Course,
    Lecture,
    PinnedSubject,
    Subject,
    SubjectFollower,
    Teacher,
    TeacherAlias,
    Topic,
    User,
)
from bot.handlers import admin
from bot.handlers.subjects import subjects_command


def _message(user_id, text):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


def _callback(user_id, data):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        data=data,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


async def _subject(session, slug, name, order=1):
    subject = Subject(slug=slug, name=name, emoji="S", order=order)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)
    return subject


async def _preview(session, subject_id):
    message = _message(123, f"/removesubject {subject_id}")
    await admin.removesubject_cmd(message, session)
    return message


async def test_removes_duplicate_subject_without_dependencies(session):
    duplicate = await _subject(session, "biochemistry-dup", "Biochemistry Duplicate")
    duplicate_id = duplicate.id

    message = await _preview(session, duplicate_id)

    text = message.answer.await_args.args[0]
    assert "REMOVE SUBJECT PREVIEW" in text
    assert f"Subject #{duplicate_id}: Biochemistry Duplicate" in text
    assert "Slug: biochemistry-dup" in text
    assert "Linked records: none" in text
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert keyboard is not None
    confirm_data = keyboard.inline_keyboard[0][0].callback_data

    callback = _callback(123, confirm_data)
    await admin.removesubject_callback(callback, session)

    edited = callback.message.edit_text.await_args.args[0]
    assert f"Deleted subject #{duplicate_id}" in edited
    assert await session.get(Subject, duplicate_id) is None
    action = await session.scalar(
        select(AdminAction).where(AdminAction.action == "remove_subject")
    )
    assert action is not None
    assert action.target_id == duplicate_id


async def test_refuses_removal_when_dependencies_exist(session):
    duplicate = await _subject(session, "biochemistry-dup", "Biochemistry Duplicate")
    user = User(id=999, username="student", full_name="Student")
    session.add(user)
    course = Course(
        year="",
        subject_id=duplicate.id,
        name="Biochemistry",
        destination_chat_id="-1004309769390",
        topic_id=8,
    )
    lecture = Lecture(subject_id=duplicate.id, title="Glycolysis", order=1)
    teacher = Teacher(
        subject_id=duplicate.id, name="Dr Enzyme", normalized_name="dr enzyme"
    )
    topic = Topic(
        subject_id=duplicate.id, name="Metabolism", normalized_name="metabolism"
    )
    session.add_all([course, lecture, teacher, topic])
    await session.flush()
    session.add_all(
        [
            TeacherAlias(
                teacher_id=teacher.id,
                subject_id=duplicate.id,
                name="Enzyme",
                normalized_name="enzyme",
            ),
            SubjectFollower(user_id=user.id, subject_id=duplicate.id),
            PinnedSubject(user_id=user.id, subject_id=duplicate.id),
        ]
    )
    await session.commit()

    message = await _preview(session, duplicate.id)

    text = message.answer.await_args.args[0]
    assert "Deletion refused" in text
    assert "Courses: 1" in text
    assert "Biochemistry" in text
    assert "Lectures: 1" in text
    assert "Glycolysis" in text
    assert "Teachers: 1" in text
    assert "Dr Enzyme" in text
    assert "Teacher aliases: 1" in text
    assert "Topics: 1" in text
    assert "Subject followers: 1" in text
    assert "Pinned subjects: 1" in text
    assert message.answer.await_args.kwargs["reply_markup"] is None
    assert await session.get(Subject, duplicate.id) is not None


async def test_confirmation_uses_exact_subject_id_and_does_not_touch_others(session):
    first = await _subject(session, "first", "First Subject")
    second = await _subject(session, "second", "Second Subject", order=2)
    first_id, second_id = first.id, second.id

    message = await _preview(session, first_id)
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    confirm_data = keyboard.inline_keyboard[0][0].callback_data

    invalid = _callback(123, "removesubject:confirm:999999")
    await admin.removesubject_callback(invalid, session)
    assert await session.get(Subject, first_id) is not None
    assert await session.get(Subject, second_id) is not None

    callback = _callback(123, confirm_data)
    await admin.removesubject_callback(callback, session)

    assert await session.get(Subject, first_id) is None
    assert await session.get(Subject, second_id) is not None


async def test_removing_duplicate_leaves_biochemistry_course_and_topic_untouched(session):
    biochemistry = await _subject(session, "biochemistry", "Biochemistry")
    biochemistry_id = biochemistry.id
    course = Course(
        id=3,
        year="",
        subject_id=biochemistry_id,
        name="Biochemistry",
        destination_chat_id="-1004309769390",
        topic_id=8,
    )
    session.add(course)
    await session.commit()
    duplicate = await _subject(
        session, "biochemistry-dup", "Biochemistry Duplicate", order=2
    )
    duplicate_id = duplicate.id

    message = await _preview(session, duplicate_id)
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    callback = _callback(123, keyboard.inline_keyboard[0][0].callback_data)
    await admin.removesubject_callback(callback, session)

    session.expire_all()
    saved_course = await session.get(Course, 3)
    assert saved_course is not None
    assert saved_course.subject_id == biochemistry_id
    assert saved_course.name == "Biochemistry"
    assert saved_course.destination_chat_id == "-1004309769390"
    assert saved_course.topic_id == 8
    assert await session.get(Subject, biochemistry_id) is not None
    assert await session.get(Subject, duplicate_id) is None


async def test_non_admin_cannot_preview_or_confirm_removal(session):
    duplicate = await _subject(session, "biochemistry-dup", "Biochemistry Duplicate")

    message = _message(999, f"/removesubject {duplicate.id}")
    await admin.removesubject_cmd(message, session)
    message.answer.assert_not_awaited()

    callback = _callback(999, f"removesubject:confirm:{duplicate.id}")
    await admin.removesubject_callback(callback, session)
    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_not_awaited()
    assert await session.get(Subject, duplicate.id) is not None


async def test_subjects_command_keeps_student_catalogue_for_normal_users(session):
    anatomy = await _subject(session, "anatomy", "Anatomy")
    await session.commit()

    message = _message(999, "/subjects")
    await subjects_command(message, session, SimpleNamespace(id=999))

    text = message.answer.await_args.args[0]
    assert "Library" in text
    assert "Choose a subject to start studying." in text
    assert "ADMIN SUBJECT INVENTORY" not in text
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    callback_data = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert f"subj:{anatomy.slug}" in callback_data


async def test_adminsubjects_lists_dependencies_and_safety_labels(session):
    blocked = await _subject(session, "anatomy", "Anatomy")
    safe = await _subject(session, "biochemistry-dup", "Biochemistry Duplicate", order=2)
    session.add_all(
        [
            Course(year="", subject_id=blocked.id, name="Anatomy A"),
            Lecture(subject_id=blocked.id, title="Thorax", order=1),
        ]
    )
    await session.commit()

    message = _message(123, "/adminsubjects")
    await admin.adminsubjects_cmd(message, session)

    text = message.answer.await_args.args[0]
    assert "ADMIN SUBJECT INVENTORY (read-only)." in text
    assert (
        f"#{blocked.id} | Anatomy | anatomy | courses=1, lectures=1 | "
        "BLOCKED - linked records"
    ) in text
    assert (
        f"#{safe.id} | Biochemistry Duplicate | biochemistry-dup | none | "
        "SAFE - no dependencies"
    ) in text
    assert "-100" not in text
    assert "topic_id" not in text


async def test_adminsubjects_is_admin_only(session):
    await _subject(session, "anatomy", "Anatomy")

    message = _message(999, "/adminsubjects")
    await admin.adminsubjects_cmd(message, session)

    message.answer.assert_not_awaited()


async def test_removesubject_validates_input(session):
    message = _message(123, "/removesubject not-a-number")
    await admin.removesubject_cmd(message, session)
    assert "Usage: /removesubject" in message.answer.await_args.args[0]

    for raw in ("0", "-1", "999999999999999999999"):
        invalid = _message(123, f"/removesubject {raw}")
        await admin.removesubject_cmd(invalid, session)
        assert "Usage: /removesubject" in invalid.answer.await_args.args[0]
