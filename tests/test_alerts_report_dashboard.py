from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select

from bot.db.ingestion_repo import approve_batch
from bot.db.models import Lecture, Progress, Subject, UploadItem, User
from bot.db.repo import (
    follow_subject,
    is_following,
    list_subject_follower_ids,
    unfollow_subject,
)
from bot.handlers.admin import dashboard_cmd
from bot.handlers.lectures import (
    ReportStates,
    report_details,
    report_reason,
    report_start,
)
from bot.handlers.subjects import cb_follow_subject
from bot.keyboards.inline import lecture_actions_kb, teachers_kb
from bot.services.access import APPROVED
from bot.services.ingestion import ingest_media
from bot.services.notifications import notify_subject_followers
from bot.services.publishing import publish_item


def _user(user_id: int, name: str = "Student") -> User:
    return User(
        id=user_id,
        username=f"user{user_id}",
        full_name=name,
        is_premium=False,
        is_admin=False,
        access_status=APPROVED,
    )


def _callback(data: str, user_id: int = 9001):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(
            id=user_id, full_name="Student", username="student"
        ),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def _message(text: str, user_id: int = 9001):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(
            id=user_id, full_name="Student", username="student"
        ),
        answer=AsyncMock(),
    )


class FakeState:
    def __init__(self):
        self.state = None
        self.data: dict = {}

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state = None
        self.data.clear()


def _buttons(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


async def _lecture(session, subject):
    lecture = Lecture(subject_id=subject.id, title="Cell Physiology", order=1)
    session.add(lecture)
    await session.commit()
    await session.refresh(lecture)
    return lecture


# ---------- Following ----------
async def test_follow_toggle_round_trip(session, seeded_course):
    subject, _ = seeded_course
    session.add(_user(9001))
    await session.commit()

    assert await is_following(session, 9001, subject.id) is False
    assert await follow_subject(session, 9001, subject.id) is True
    assert await is_following(session, 9001, subject.id) is True
    assert await list_subject_follower_ids(session, subject.id) == [9001]

    assert await follow_subject(session, 9001, subject.id) is True
    assert await list_subject_follower_ids(session, subject.id) == [9001]

    assert await unfollow_subject(session, 9001, subject.id) is True
    assert await is_following(session, 9001, subject.id) is False


async def test_follow_callback_toggles_and_rerenders(session, seeded_course):
    subject, _ = seeded_course
    session.add(_user(9001))
    await session.commit()

    cb = _callback(f"follow:{subject.id}")
    await cb_follow_subject(cb, session, _user(9001))
    assert await is_following(session, 9001, subject.id) is True
    cb.message.edit_text.assert_awaited()
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert f"follow:{subject.id}" in _buttons(markup)

    await cb_follow_subject(cb, session, _user(9001))
    assert await is_following(session, 9001, subject.id) is False


async def test_teachers_keyboard_shows_alert_state(session, seeded_course):
    subject, _ = seeded_course
    followed = teachers_kb([], subject.id, following=True)
    not_followed = teachers_kb([], subject.id, following=False)
    assert f"follow:{subject.id}" in _buttons(followed)
    assert f"follow:{subject.id}" in _buttons(not_followed)
    labels = {b.text for row in followed.inline_keyboard for b in row}
    other = {b.text for row in not_followed.inline_keyboard for b in row}
    assert labels != other


# ---------- Notification delivery ----------
async def test_notify_followers_sends_and_unfollows_blocked(session, seeded_course):
    subject, _ = seeded_course
    session.add_all([_user(9001), _user(9002)])
    await session.commit()
    await follow_subject(session, 9001, subject.id)
    await follow_subject(session, 9002, subject.id)
    lecture = await _lecture(session, subject)

    bot = AsyncMock()

    async def send(chat_id, *args, **kwargs):
        if chat_id == 9002:
            raise TelegramForbiddenError(method=None, message="blocked")
        return SimpleNamespace(message_id=1)

    bot.send_message.side_effect = send

    sent = await notify_subject_followers(session, bot, lecture)

    assert sent == 1
    assert await is_following(session, 9001, subject.id) is True
    assert await is_following(session, 9002, subject.id) is False


async def test_publishing_notifies_followers_only_once(session, seeded_course):
    subject, course = seeded_course
    session.add(_user(9001))
    await session.commit()
    await follow_subject(session, 9001, subject.id)

    course.destination_chat_id = "-100123"
    course.topic_id = 8
    await session.commit()

    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=501,
        media_type="video",
        caption="Dr A Anatomy Lecture 1 - Thoracic wall",
        file_id="file-501",
        file_unique_id="unique-501",
        file_name=None,
    )
    ok, text = await approve_batch(session, item.batch_id, 123)
    assert ok is True, text

    bot = AsyncMock()
    bot.send_video.return_value = SimpleNamespace(message_id=700)
    result = await publish_item(session, bot, item.id)
    assert result.status == "published"
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.args[0] == 9001

    result = await publish_item(session, bot, item.id)
    assert result.status == "already_published"
    assert bot.send_message.await_count == 1


# ---------- Report ----------
async def test_report_flow_alerts_admins(session, seeded_course):
    subject, _ = seeded_course
    session.add(_user(9001))
    await session.commit()
    lecture = await _lecture(session, subject)

    cb = _callback(f"report:{lecture.id}")
    await report_start(cb, session, _user(9001))
    buttons = _buttons(cb.message.edit_text.await_args.kwargs["reply_markup"])
    assert f"report2:{lecture.id}:broken" in buttons
    assert f"report2:{lecture.id}:other" in buttons
    assert f"report2:{lecture.id}:cancel" in buttons

    state = FakeState()
    bot = AsyncMock()
    cb_reason = _callback(f"report2:{lecture.id}:broken")
    await report_reason(cb_reason, session, state, bot)
    assert state.state == ReportStates.waiting_details
    assert state.data["lecture_id"] == lecture.id

    msg = _message("The video stops halfway.")
    await report_details(msg, session, state, bot)
    assert state.state is None
    assert bot.send_message.await_count == 1
    admin_id, alert = bot.send_message.await_args.args[:2]
    assert admin_id == 123  # ADMIN_IDS owner
    assert "Lecture report" in alert
    assert "Video is broken" in alert
    assert "The video stops halfway." in alert
    msg.answer.assert_awaited_once()


async def test_report_skip_sends_without_details(session, seeded_course):
    subject, _ = seeded_course
    session.add(_user(9001))
    await session.commit()
    lecture = await _lecture(session, subject)

    state = FakeState()
    await state.set_state(ReportStates.waiting_details)
    await state.update_data(lecture_id=lecture.id, reason="Something else")

    bot = AsyncMock()
    cb = _callback(f"report2:{lecture.id}:skip")
    await report_reason(cb, session, state, bot)

    assert state.state is None
    assert bot.send_message.await_count == 1
    assert "Something else" in bot.send_message.await_args.args[1]
    cb.message.edit_text.assert_awaited()


async def test_report_cancel_clears_state(session, seeded_course):
    subject, _ = seeded_course
    lecture = await _lecture(session, subject)
    state = FakeState()
    await state.set_state(ReportStates.waiting_details)
    await state.update_data(lecture_id=lecture.id, reason="x")

    bot = AsyncMock()
    cb = _callback(f"report2:{lecture.id}:cancel")
    await report_reason(cb, session, state, bot)

    assert state.state is None
    bot.send_message.assert_not_awaited()
    cb.message.edit_text.assert_awaited_with("Report cancelled.")


async def test_lecture_actions_include_report_button(seeded_course):
    markup = lecture_actions_kb(77, False, False, False)
    assert "report:77" in _buttons(markup)


# ---------- Dashboard ----------
async def test_dashboard_reports_activity(session, seeded_course):
    subject, _ = seeded_course
    now = datetime.utcnow()
    session.add_all(
        [
            User(
                id=9001,
                full_name="Active",
                access_status=APPROVED,
                last_active=now,
            ),
            User(
                id=9002,
                full_name="Idle",
                access_status=APPROVED,
                last_active=now - timedelta(days=30),
                created_at=now - timedelta(days=60),
            ),
        ]
    )
    lecture = Lecture(subject_id=subject.id, title="Cell Physiology", order=1)
    session.add(lecture)
    await session.commit()
    await session.refresh(lecture)
    session.add(
        Progress(
            user_id=9001,
            lecture_id=lecture.id,
            watched=True,
            watched_at=now,
        )
    )
    await session.commit()

    msg = _message("/dashboard", user_id=123)
    await dashboard_cmd(msg, session)
    out = msg.answer.await_args.args[0]

    assert "Dashboard" in out
    assert "• Total: 2" in out
    assert "• Active today: 1" in out
    assert "• Published: 1" in out
    assert "• Today: 1" in out
    assert "Anatomy — 1 views" in out


async def test_dashboard_ignores_non_admins(session):
    msg = _message("/dashboard", user_id=9001)
    await dashboard_cmd(msg, session)
    msg.answer.assert_not_awaited()
