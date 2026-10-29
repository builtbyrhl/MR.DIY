from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from aiogram.exceptions import TelegramForbiddenError

from bot.db.models import Lecture, Progress, Subject, User
from bot.db.repo import up_next_lectures
from bot.handlers.progress import progress_entry, reminder_toggle
from bot.services.access import APPROVED
from bot.services.reminders import send_daily_reminders


@pytest_asyncio.fixture
async def catalogue(session):
    anatomy = Subject(slug="anatomy", name="Anatomy", emoji="A", order=1)
    physiology = Subject(slug="physiology", name="Physiology", emoji="P", order=2)
    session.add_all([anatomy, physiology])
    await session.commit()
    await session.refresh(anatomy)
    await session.refresh(physiology)

    a1 = Lecture(subject_id=anatomy.id, title="Thorax", order=1)
    a2 = Lecture(subject_id=anatomy.id, title="Abdomen", order=2)
    p1 = Lecture(subject_id=physiology.id, title="Cells", order=1)
    session.add_all([a1, a2, p1])
    await session.commit()
    for lec in (a1, a2, p1):
        await session.refresh(lec)

    user = User(
        id=9001,
        username="student",
        full_name="Student",
        is_premium=False,
        is_admin=False,
        access_status=APPROVED,
        streak=0,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {
        "anatomy": anatomy,
        "physiology": physiology,
        "a1": a1,
        "a2": a2,
        "p1": p1,
        "user": user,
    }


async def _watch(session, user_id: int, lecture_id: int):
    session.add(
        Progress(
            user_id=user_id,
            lecture_id=lecture_id,
            watched=True,
            watched_at=datetime.utcnow(),
        )
    )
    await session.commit()


def _message(user_id: int = 9001):
    return SimpleNamespace(
        text="📈 Progress",
        from_user=SimpleNamespace(id=user_id, full_name="Student"),
        answer=AsyncMock(),
    )


def _callback(data: str, user_id: int = 9001):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Student"),
        message=SimpleNamespace(
            edit_text=AsyncMock(), text="📈 Progress"
        ),
        answer=AsyncMock(),
    )


def _buttons(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


async def test_up_next_returns_first_unwatched_per_started_subject(
    session, catalogue
):
    user = catalogue["user"]
    await _watch(session, user.id, catalogue["a1"].id)

    picks = await up_next_lectures(session, user.id)
    assert [(s.slug, l.id) for s, l in picks] == [
        ("anatomy", catalogue["a2"].id)
    ]

    await _watch(session, user.id, catalogue["p1"].id)
    picks = await up_next_lectures(session, user.id)
    assert [l.id for _, l in picks] == [catalogue["a2"].id]

    await _watch(session, user.id, catalogue["a2"].id)
    assert await up_next_lectures(session, user.id) == []


async def test_up_next_skips_premium_for_free_users(session, catalogue):
    user = catalogue["user"]
    catalogue["a2"].is_premium = True
    await session.commit()
    await _watch(session, user.id, catalogue["a1"].id)

    assert await up_next_lectures(session, user.id, include_premium=False) == []
    picks = await up_next_lectures(session, user.id, include_premium=True)
    assert [l.id for _, l in picks] == [catalogue["a2"].id]


async def test_progress_page_shows_up_next_and_reminder_toggle(
    session, catalogue
):
    user = catalogue["user"]
    await _watch(session, user.id, catalogue["a1"].id)

    message = _message(user.id)
    await progress_entry(message, session, user)

    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "Up next" in text
    assert "Abdomen" in text
    buttons = _buttons(markup)
    assert f"lec:{catalogue['a2'].id}" in buttons
    assert "reminder:toggle" in buttons


async def test_reminder_toggle_flips_flag_and_rerenders(session, catalogue):
    user = catalogue["user"]
    cb = _callback("reminder:toggle")

    await reminder_toggle(cb, session, user)
    await session.refresh(user)
    assert user.reminder_enabled is True
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    labels = {
        button.callback_data: button.text
        for row in markup.inline_keyboard
        for button in row
    }
    assert "ON" in labels["reminder:toggle"]

    await reminder_toggle(cb, session, user)
    await session.refresh(user)
    assert user.reminder_enabled is False


async def test_reminder_sent_once_per_day(session, catalogue, db):
    _, factory = db
    user = catalogue["user"]
    user.reminder_enabled = True
    await session.commit()
    await _watch(session, user.id, catalogue["a1"].id)

    bot = AsyncMock()
    now = datetime(2026, 5, 4, 14, 30)

    sent = await send_daily_reminders(bot, now=now, session_factory=factory)
    assert sent == 1
    bot.send_message.assert_awaited_once()
    chat_id, text = bot.send_message.await_args.args[:2]
    assert chat_id == user.id
    assert "Study reminder" in text
    assert "Abdomen" in text
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert f"lec:{catalogue['a2'].id}" in _buttons(markup)

    await session.refresh(user)
    assert user.last_reminder_date == date(2026, 5, 4)

    sent = await send_daily_reminders(bot, now=now, session_factory=factory)
    assert sent == 0
    assert bot.send_message.await_count == 1


async def test_reminder_skips_users_who_never_opted_in(session, catalogue, db):
    _, factory = db
    bot = AsyncMock()
    sent = await send_daily_reminders(
        bot, now=datetime(2026, 5, 4, 14, 30), session_factory=factory
    )
    assert sent == 0
    bot.send_message.assert_not_awaited()


async def test_reminder_disabled_when_bot_blocked(session, catalogue, db):
    _, factory = db
    user = catalogue["user"]
    user.reminder_enabled = True
    await session.commit()

    bot = AsyncMock()
    bot.send_message.side_effect = TelegramForbiddenError(
        method=None, message="bot was blocked"
    )

    sent = await send_daily_reminders(
        bot, now=datetime(2026, 5, 4, 14, 30), session_factory=factory
    )
    assert sent == 0
    await session.refresh(user)
    assert user.reminder_enabled is False
