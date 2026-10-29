from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from aiogram.exceptions import TelegramForbiddenError

from bot.db.models import AdminAction, Lecture, Progress, Subject, User
from bot.db.repo import list_admin_actions, record_admin_action
from bot.handlers.access import approve_user_cmd
from bot.handlers.admin import addadmin_cmd, log_cmd
from bot.handlers.progress import digest_toggle, progress_entry
from bot.services.access import APPROVED
from bot.services.reminders import send_weekly_digests


@pytest_asyncio.fixture
async def catalogue(session):
    anatomy = Subject(slug="anatomy", name="Anatomy", emoji="A", order=1)
    session.add(anatomy)
    await session.commit()
    await session.refresh(anatomy)

    a1 = Lecture(subject_id=anatomy.id, title="Thorax", order=1)
    a2 = Lecture(subject_id=anatomy.id, title="Abdomen", order=2)
    session.add_all([a1, a2])
    await session.commit()
    for lec in (a1, a2):
        await session.refresh(lec)

    user = User(
        id=9001,
        username="student",
        full_name="Student",
        is_premium=False,
        is_admin=False,
        access_status=APPROVED,
        streak=4,
        last_study_date=datetime.utcnow().date(),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {"subject": anatomy, "a1": a1, "a2": a2, "user": user}


def _message(text, user_id=9001, chat_type="private"):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id, full_name="Admin"),
        chat=SimpleNamespace(type=chat_type),
        answer=AsyncMock(),
    )


def _callback(data, user_id=9001):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Student"),
        message=SimpleNamespace(edit_text=AsyncMock(), text="📈 Progress"),
        answer=AsyncMock(),
    )


def _buttons(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


async def test_send_weekly_digests_once_per_day(session, catalogue, db):
    _, factory = db
    user = catalogue["user"]
    user.digest_enabled = True
    session.add(
        Progress(
            user_id=user.id,
            lecture_id=catalogue["a1"].id,
            watched=True,
            watched_at=datetime.utcnow(),
        )
    )
    await session.commit()

    bot = AsyncMock()
    now = datetime.utcnow()

    sent = await send_weekly_digests(bot, now=now, session_factory=factory)
    assert sent == 1
    chat_id, text = bot.send_message.await_args.args[:2]
    assert chat_id == user.id
    assert "week in review" in text
    assert "Streak: 4" in text
    assert "Watched this week: 1" in text
    assert "Anatomy" in text
    assert "Up next: Anatomy — Abdomen" in text
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert f"lec:{catalogue['a2'].id}" in _buttons(markup)

    await session.refresh(user)
    assert user.last_digest_date == now.date()

    sent = await send_weekly_digests(bot, now=now, session_factory=factory)
    assert sent == 0
    assert bot.send_message.await_count == 1


async def test_digest_skips_users_who_never_opted_in(session, catalogue, db):
    _, factory = db
    bot = AsyncMock()
    sent = await send_weekly_digests(
        bot, now=datetime(2026, 5, 3, 14, 30), session_factory=factory
    )
    assert sent == 0
    bot.send_message.assert_not_awaited()


async def test_digest_disabled_when_bot_blocked(session, catalogue, db):
    _, factory = db
    user = catalogue["user"]
    user.digest_enabled = True
    await session.commit()

    bot = AsyncMock()
    bot.send_message.side_effect = TelegramForbiddenError(
        method=None, message="blocked"
    )

    sent = await send_weekly_digests(
        bot, now=datetime(2026, 5, 3, 14, 30), session_factory=factory
    )
    assert sent == 0
    await session.refresh(user)
    assert user.digest_enabled is False


async def test_progress_page_has_digest_toggle(session, catalogue):
    user = catalogue["user"]
    message = _message("📈 Progress", user.id)
    await progress_entry(message, session, user)
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "digest:toggle" in _buttons(markup)

    cb = _callback("digest:toggle")
    await digest_toggle(cb, session, user)
    await session.refresh(user)
    assert user.digest_enabled is True


# ---------- Audit log ----------
async def test_record_and_list_admin_actions(session):
    await record_admin_action(
        session, 123, "approve_batch", target_type="batch", target_id=5
    )
    await record_admin_action(
        session, 123, "publish_batch", target_type="batch", target_id=5,
        details="2 published",
    )
    actions = await list_admin_actions(session, 10)
    assert [a.action for a in actions] == ["publish_batch", "approve_batch"]
    assert actions[0].details == "2 published"
    assert actions[0].admin_id == 123


async def test_approve_access_command_writes_audit_row(session):
    msg = _message("/approveuser 5555 30", user_id=123)
    await approve_user_cmd(msg, session, AsyncMock())

    actions = await list_admin_actions(session, 10)
    assert actions[0].action == "approve_access"
    assert actions[0].target_id == 5555
    assert actions[0].details == "days=30"


async def test_addadmin_writes_audit_row(session):
    session.add(
        User(id=7777, full_name="New Admin", access_status=APPROVED)
    )
    await session.commit()
    msg = _message("/addadmin 7777", user_id=123)
    await addadmin_cmd(msg, session)

    actions = await list_admin_actions(session, 10)
    assert actions[0].action == "add_admin"
    assert actions[0].target_id == 7777


async def test_log_command_renders_actions(session):
    await record_admin_action(
        session, 123, "move_items", target_type="batch", target_id=9,
        details="moved=3",
    )
    msg = _message("/log 10", user_id=123)
    await log_cmd(msg, session)
    out = msg.answer.await_args.args[0]
    assert "Admin activity" in out
    assert "123 move_items batch #9" in out
    assert "moved=3" in out


async def test_log_command_rejects_non_admins(session):
    msg = _message("/log", user_id=9001)
    await log_cmd(msg, session)
    msg.answer.assert_not_awaited()


async def test_audit_failure_does_not_break_callers(session, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(session, "commit", boom)
    await record_admin_action(session, 123, "approve_batch")
