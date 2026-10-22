from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from bot.db.models import Lecture, Subject, User
from bot.handlers import admin


def _message(user_id, text):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


async def test_addlecture_rejects_non_numeric_message_id(session):
    subject = Subject(slug="anatomy", name="Anatomy", emoji="A", order=1)
    session.add(subject)
    await session.commit()

    message = _message(123, "/addlecture anatomy|Lecture 1|not-a-number")
    await admin.add_lecture(message, session)

    message.answer.assert_awaited_once()
    assert "must be a number" in message.answer.await_args.args[0]
    assert await session.scalar(select(Lecture)) is None


async def test_addlecture_rejects_negative_message_id(session):
    subject = Subject(slug="anatomy", name="Anatomy", emoji="A", order=1)
    session.add(subject)
    await session.commit()

    message = _message(123, "/addlecture anatomy|Lecture 1|-99")
    await admin.add_lecture(message, session)

    message.answer.assert_awaited_once()
    assert "positive" in message.answer.await_args.args[0]
    assert await session.scalar(select(Lecture)) is None


async def test_addsubject_rejects_duplicate_slug(session):
    first = _message(123, "/addsubject anatomy|Anatomy|A")
    await admin.add_subject(first, session)
    first.answer.assert_awaited_once()

    second = _message(123, "/addsubject anatomy|Other Anatomy|B")
    await admin.add_subject(second, session)

    second.answer.assert_awaited_once()
    assert "already exists" in second.answer.await_args.args[0]
    assert await session.scalar(select(Subject)) is not None


async def test_grant_reports_missing_user_without_creating_one(session):
    message = _message(123, "/grant 9999 30")
    await admin.grant_cmd(message, session)

    message.answer.assert_awaited_once()
    assert "not found" in message.answer.await_args.args[0]
    assert await session.get(User, 9999) is None


async def test_grant_rejects_invalid_input(session):
    message = _message(123, "/grant 123 abc")
    await admin.grant_cmd(message, session)

    message.answer.assert_awaited_once()
    assert "Usage" in message.answer.await_args.args[0]

    negative = _message(123, "/grant -5 30")
    await admin.grant_cmd(negative, session)
    assert "positive" in negative.answer.await_args.args[0]


async def test_grant_extends_existing_user_premium(session):
    user = User(id=9998, username="student", full_name="Student")
    session.add(user)
    await session.commit()

    message = _message(123, "/grant 9998 30")
    await admin.grant_cmd(message, session)

    await session.refresh(user)
    assert user.is_premium is True
    assert user.premium_until is not None
    message.answer.assert_awaited_once()
    assert "Granted premium" in message.answer.await_args.args[0]
