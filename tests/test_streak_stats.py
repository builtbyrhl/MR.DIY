"""Tests for study streak and stats."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.db.models import Lecture, Subject, User
from bot.db.repo import active_streak, count_watched, mark_watched
from bot.handlers import progress as progress_handler


async def _user(session, user_id: int = 4400) -> User:
    user = User(id=user_id)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _lecture(session, subject: Subject, title: str = "Lec") -> Lecture:
    lecture = Lecture(
        subject_id=subject.id,
        title=title,
        channel_message_id=100,
        order=1,
    )
    session.add(lecture)
    await session.commit()
    await session.refresh(lecture)
    return lecture


async def test_streak_starts_repeats_and_increments(session):
    user = await _user(session)
    subject = Subject(slug="anat", name="Anatomy", emoji="A")
    session.add(subject)
    await session.commit()
    await session.refresh(subject)
    lecture = await _lecture(session, subject)

    today = datetime.utcnow().date()

    await mark_watched(session, user.id, lecture.id)
    await session.refresh(user)
    assert user.streak == 1
    assert user.last_study_date == today

    # Same day again: streak unchanged.
    await mark_watched(session, user.id, lecture.id)
    await session.refresh(user)
    assert user.streak == 1

    # Next consecutive day: streak grows.
    user.last_study_date = today - timedelta(days=1)
    await session.commit()
    await mark_watched(session, user.id, lecture.id)
    await session.refresh(user)
    assert user.streak == 2

    # Gap: streak resets to one.
    user.last_study_date = today - timedelta(days=3)
    await session.commit()
    await mark_watched(session, user.id, lecture.id)
    await session.refresh(user)
    assert user.streak == 1


def test_active_streak_zero_after_gap():
    today = date.today()
    user = SimpleNamespace(streak=5, last_study_date=today - timedelta(days=3))
    assert active_streak(user, today) == 0

    user = SimpleNamespace(streak=5, last_study_date=today - timedelta(days=1))
    assert active_streak(user, today) == 5

    user = SimpleNamespace(streak=5, last_study_date=today)
    assert active_streak(user, today) == 5

    user = SimpleNamespace(streak=0, last_study_date=None)
    assert active_streak(user, today) == 0


async def test_progress_page_shows_streak_and_watch_counts(session):
    user = await _user(session)
    subject = Subject(slug="anat", name="Anatomy", emoji="A")
    session.add(subject)
    await session.commit()
    await session.refresh(subject)
    lecture = await _lecture(session, subject, title="First Lecture")

    await mark_watched(session, user.id, lecture.id)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=user.id), answer=AsyncMock()
    )
    await progress_handler.progress_entry(message, session, user)

    text = message.answer.await_args.args[0]
    assert "Study streak: 1 day" in text
    assert "Watched: 1 total · 1 today · 1 this week" in text
    assert "📈 *Your progress*" in text

    assert await count_watched(session, user.id) == 1
