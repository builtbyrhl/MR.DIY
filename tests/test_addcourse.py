from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from sqlalchemy import select

from bot.db.models import Course, Subject


@pytest_asyncio.fixture
async def anatomy_subject(session):
    """A subject without any course, so course rows are created by the test."""
    subject = Subject(slug="anatomy", name="Anatomy", emoji="\U0001FAC0", order=1)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)
    return subject


def _message(user_id, text):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


async def _add(session, text, user_id=123):
    from bot.handlers.review import addcourse_cmd

    message = _message(user_id, text)
    await addcourse_cmd(message, session)
    return message


async def test_addcourse_uses_subject_first_syntax(session, anatomy_subject):
    subject = anatomy_subject

    message = await _add(session, "/addcourse anatomy|Anatomy|anatomy,anat|-1009999999999|42")

    message.answer.assert_awaited_once()
    reply = message.answer.await_args.args[0]
    assert "Added course" in reply
    assert "Anatomy" in reply
    assert "-1009999999999" in reply
    assert "42" in reply

    course = await session.scalar(select(Course))
    assert course is not None
    assert course.subject_id == subject.id
    assert course.name == "Anatomy"
    assert course.aliases == "anatomy,anat"
    assert course.destination_chat_id == "-1009999999999"
    assert course.topic_id == 42
    assert course.year == ""


async def test_addcourse_defaults_name_and_aliases_to_subject(session, anatomy_subject):
    subject = anatomy_subject

    await _add(session, "/addcourse anatomy")

    course = await session.scalar(select(Course))
    assert course is not None
    assert course.subject_id == subject.id
    assert course.name == subject.name
    assert course.aliases == "anatomy"
    assert course.year == ""


async def test_addcourse_destination_falls_back_to_env(monkeypatch, session, anatomy_subject):
    from bot.handlers import review

    monkeypatch.setattr(
        review,
        "get_settings",
        lambda: SimpleNamespace(destination_group_id="-1007777777777"),
    )

    await _add(session, "/addcourse anatomy|Anatomy")

    course = await session.scalar(select(Course))
    assert course is not None
    assert course.destination_chat_id == "-1007777777777"


async def test_addcourse_rejects_unknown_subject(session, anatomy_subject):
    message = await _add(session, "/addcourse not_a_subject|Anatomy")

    message.answer.assert_awaited_once()
    assert "not found" in message.answer.await_args.args[0]
    assert await session.scalar(select(Course)) is None


async def test_addcourse_rejects_non_numeric_topic(session, anatomy_subject):
    message = await _add(session, "/addcourse anatomy|Anatomy|anat|-1009999999999|abc")

    message.answer.assert_awaited_once()
    assert "Topic ID must be a number" in message.answer.await_args.args[0]
    assert await session.scalar(select(Course)) is None


async def test_addcourse_updates_existing_course_instead_of_duplicating(session, anatomy_subject):
    await _add(session, "/addcourse anatomy|Anatomy|anat")
    message = await _add(session, "/addcourse anatomy|Anatomy|anat|-1009999999999|42")

    courses = (await session.scalars(select(Course))).all()
    assert len(courses) == 1
    assert courses[0].destination_chat_id == "-1009999999999"
    assert courses[0].topic_id == 42
    assert "Updated course" in message.answer.await_args.args[0]


async def test_addcourse_preserves_legacy_year_and_topic(session, anatomy_subject):
    subject = anatomy_subject
    course = Course(
        year="1st Year",
        subject_id=subject.id,
        name="Dr A Anatomy",
        aliases="dr a,anatomy a",
        destination_chat_id="-1005555555555",
        topic_id=7,
    )
    session.add(course)
    await session.commit()

    await _add(session, "/addcourse anatomy|Dr A Anatomy|dr a,anatomy a")

    stored = await session.scalar(select(Course))
    assert stored is not None
    assert stored.year == "1st Year"
    assert stored.topic_id == 7
    assert stored.destination_chat_id == "-1005555555555"


async def test_addcourse_is_admin_only(session, anatomy_subject):
    message = await _add(session, "/addcourse anatomy|Anatomy", user_id=999)

    message.answer.assert_not_awaited()
    assert await session.scalar(select(Course)) is None


async def test_courses_listing_shows_subject(session, anatomy_subject):
    from bot.handlers.review import courses_cmd

    await _add(session, "/addcourse anatomy|Anatomy|anat|-1009999999999|42")

    message = _message(123, "/courses")
    await courses_cmd(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "Anatomy" in text
    assert "-1009999999999" in text
    assert "42" in text
