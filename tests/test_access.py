from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.db.models import Lecture, Subject, User
from bot.db.repo import get_or_create_user
from bot.handlers.access import (
    access_decision_cb,
    access_list_cmd,
    approve_user_cmd,
    my_access_cmd,
    reject_user_cmd,
    request_access_cb,
    revoke_user_cmd,
)
from bot.handlers.lectures import open_lecture, watch_lecture
from bot.handlers.start import cmd_start
from bot.middlewares.db import DbMiddleware
from bot.services.access import (
    APPROVED,
    PENDING,
    REJECTED,
    access_expires_at,
    access_is_expired,
    access_status,
    approve_access,
    has_library_access,
    request_access,
    revoke_access,
)


def _tg(user_id=9001, **overrides):
    data = {
        "id": user_id,
        "username": f"user{user_id}",
        "full_name": f"User {user_id}",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _message(user_id=9001, text=""):
    return SimpleNamespace(
        from_user=_tg(user_id),
        chat=SimpleNamespace(id=user_id, username=None, type="private"),
        text=text,
        answer=AsyncMock(),
    )


def _callback(data, user_id=9001, message_text=None):
    return SimpleNamespace(
        data=data,
        from_user=_tg(user_id),
        message=SimpleNamespace(
            edit_text=AsyncMock(),
            text=message_text,
            chat=SimpleNamespace(id=user_id, username=None, type="private"),
        ),
        answer=AsyncMock(),
    )


def _button_data(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


async def _add_user(session, user_id=9001, status=PENDING, **values):
    user = User(
        id=user_id,
        username=f"user{user_id}",
        full_name=f"User {user_id}",
        is_premium=False,
        is_admin=False,
        access_status=status,
        **values,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_new_user_defaults_to_pending(session):
    user = await get_or_create_user(session, _tg(9100))

    assert access_status(user) == PENDING
    assert not has_library_access(user)


async def test_request_access_is_idempotent(session):
    user = await _add_user(session, status=REJECTED)

    assert await request_access(session, user.id) is True
    await session.refresh(user)
    assert access_status(user) == PENDING
    assert user.access_requested_at is not None
    assert user.access_decided_at is None
    assert user.access_decided_by is None

    assert await request_access(session, user.id) is False
    assert await request_access(session, user.id) is False


async def test_approval_revocation_and_premium_are_independent(session):
    user = await _add_user(session, status=PENDING)

    approved = await approve_access(session, user.id, 123)
    assert access_status(approved) == APPROVED
    assert approved.access_decided_by == 123
    assert approved.access_decided_at is not None
    assert approved.is_premium is False
    assert has_library_access(approved) is True

    revoked = await revoke_access(session, user.id, 123)
    assert access_status(revoked) == REJECTED
    assert has_library_access(revoked) is False
    assert revoked.is_premium is False


async def test_manual_approval_creates_missing_user(session):
    user = await approve_access(session, 7777, 123, create_missing=True)

    assert user is not None
    assert user.id == 7777
    assert access_status(user) == APPROVED
    assert user.access_decided_by == 123


async def test_expired_access_blocks_library_and_can_be_renewed(session):
    user = await _add_user(
        session,
        status=APPROVED,
        access_expires_at=datetime.utcnow() - timedelta(minutes=1),
    )

    assert access_is_expired(user) is True
    assert has_library_access(user) is False

    assert await request_access(session, user.id) is True
    await session.refresh(user)
    assert access_status(user) == PENDING
    assert access_expires_at(user) is None

    renewed = await approve_access(session, user.id, 123, days=2)
    assert has_library_access(renewed) is True
    assert access_is_expired(renewed) is False
    assert renewed.access_expires_at > datetime.utcnow() + timedelta(days=1, hours=23)


async def test_lifetime_approval_has_no_expiry_and_revocation_clears_it(session):
    user = await approve_access(session, 7778, 123, create_missing=True, days=0)

    assert access_expires_at(user) is None
    assert has_library_access(user) is True

    user = await approve_access(session, 7778, 123, days=3)
    assert access_expires_at(user) is not None

    user = await revoke_access(session, 7778, 123)
    assert access_expires_at(user) is None
    assert has_library_access(user) is False


async def test_access_list_shows_pending_and_expiring_users(session):
    pending = await _add_user(session, user_id=7781, status=PENDING)
    expiring = await _add_user(
        session,
        user_id=7782,
        status=APPROVED,
        access_expires_at=datetime.utcnow() + timedelta(days=2),
    )
    message = _message(123, "/access")

    await access_list_cmd(message, session)

    text = message.answer.await_args.args[0]
    assert f"({pending.id})" in text
    assert f"({expiring.id})" in text
    assert "expiring in the next 7 days" in text.lower()


async def test_my_access_distinguishes_requested_and_unrequested(session):
    unrequested = await _add_user(session, user_id=7784, status=PENDING)
    message = _message(unrequested.id)
    await my_access_cmd(message, unrequested)
    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "not been approved" in text
    assert _button_data(markup) == ["request_access"]

    requested = await _add_user(
        session,
        user_id=7785,
        status=PENDING,
        access_requested_at=datetime.utcnow(),
    )
    pending_message = _message(requested.id)
    await my_access_cmd(pending_message, requested)
    text = pending_message.answer.await_args.args[0]
    markup = pending_message.answer.await_args.kwargs["reply_markup"]
    assert "awaiting admin review" in text
    assert _button_data(markup) == ["request_access"]


async def test_my_access_shows_expired_status_and_renewal_button(session):
    user = await _add_user(
        session,
        status=APPROVED,
        access_expires_at=datetime.utcnow() - timedelta(hours=1),
    )
    message = _message(user.id)

    await my_access_cmd(message, user)

    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "expired" in text.lower()
    assert _button_data(markup) == ["request_access"]


async def test_approve_command_accepts_optional_days(session):
    bot = AsyncMock()
    message = _message(123, "/approveuser 7780 7")

    await approve_user_cmd(message, session, bot)

    user = await session.get(User, 7780)
    assert access_status(user) == APPROVED
    assert access_is_expired(user) is False
    assert user.access_expires_at > datetime.utcnow() + timedelta(days=6)
    assert "for 7 days" in message.answer.await_args.args[0]

    lifetime_message = _message(123, "/approveuser 7783 0")
    await approve_user_cmd(lifetime_message, session, bot)
    lifetime_user = await session.get(User, 7783)
    assert access_expires_at(lifetime_user) is None
    assert "lifetime access" in lifetime_message.answer.await_args.args[0]


@pytest.mark.parametrize("status", [PENDING, REJECTED, None])
async def test_start_unapproved_shows_request_button_not_menu(session, status):
    user = await _add_user(session, status=status)
    message = _message(user.id)

    await cmd_start(message, session, user)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "Request Access" in str(markup)
    assert _button_data(markup) == ["request_access"]
    assert "FMGE Lecture Bot" in text
    if status == PENDING:
        assert "pending" in text.lower()


async def test_start_approved_shows_student_menu(session):
    user = await _add_user(session, status=APPROVED)
    message = _message(user.id)

    await cmd_start(message, session, user)

    markup = message.answer.await_args.kwargs["reply_markup"]
    labels = [button.text for row in markup.keyboard for button in row]
    assert "📚 My Courses" in labels
    assert "▶️ Continue" in labels
    assert "🔑 My Access" in labels
    assert "🔍 Search" in labels


async def test_request_access_callback_notifies_admin_only_once(session):
    user = await _add_user(session, status=REJECTED)
    bot = AsyncMock()

    cb = _callback("request_access", user.id)
    await request_access_cb(cb, session, user, bot)

    await session.refresh(user)
    assert access_status(user) == PENDING
    bot.send_message.assert_awaited_once()
    admin_id = bot.send_message.await_args.args[0]
    assert admin_id == 123
    assert "New library access request" in bot.send_message.await_args.args[1]
    cb.message.edit_text.assert_awaited_once()
    cb.answer.assert_awaited_once_with("Request sent")

    bot.reset_mock()
    again = _callback("request_access", user.id)
    await request_access_cb(again, session, user, bot)

    bot.send_message.assert_not_awaited()
    again.answer.assert_awaited_once_with("Request pending")


async def test_admin_pending_list_and_admin_only_gate(session):
    await _add_user(session, user_id=9002, status=PENDING)
    await _add_user(session, user_id=9003, status=PENDING)

    admin_message = _message(123, "/access")
    await access_list_cmd(admin_message, session)

    admin_message.answer.assert_awaited_once()
    text = admin_message.answer.await_args.args[0]
    markup = admin_message.answer.await_args.kwargs["reply_markup"]
    assert "Pending access requests:" in text
    assert "access:approve:9002" in _button_data(markup)
    assert "access:reject:9003" in _button_data(markup)

    user_message = _message(9002, "/access")
    await access_list_cmd(user_message, session)
    user_message.answer.assert_not_awaited()


async def test_admin_callback_approves_and_non_admin_cannot(session):
    user = await _add_user(session, user_id=9004, status=PENDING)
    bot = AsyncMock()

    unauthorized = _callback("access:approve:9004", user_id=9999)
    await access_decision_cb(unauthorized, session, bot)
    unauthorized.answer.assert_awaited_once_with("Admin only.", show_alert=True)
    await session.refresh(user)
    assert access_status(user) == PENDING

    cb = _callback(
        "access:approve:9004",
        user_id=123,
        message_text="New library access request",
    )
    await access_decision_cb(cb, session, bot)

    await session.refresh(user)
    assert access_status(user) == APPROVED
    assert user.access_decided_by == 123
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == user.id
    cb.answer.assert_awaited_once_with("Access approved.")


async def test_manual_approve_reject_and_revoke_commands(session):
    bot = AsyncMock()

    approve_message = _message(123, "/approveuser 7700")
    await approve_user_cmd(approve_message, session, bot)
    approved = await session.get(User, 7700)
    assert access_status(approved) == APPROVED
    assert approved.access_decided_by == 123
    approve_message.answer.assert_awaited_once()

    rejected_user = await _add_user(session, user_id=7701, status=PENDING)
    reject_message = _message(123, "/rejectuser 7701")
    await reject_user_cmd(reject_message, session, bot)
    await session.refresh(rejected_user)
    assert access_status(rejected_user) == REJECTED

    await session.refresh(approved)
    revoke_message = _message(123, "/revokeuser 7700")
    await revoke_user_cmd(revoke_message, session, bot)
    await session.refresh(approved)
    assert access_status(approved) == REJECTED
    assert has_library_access(approved) is False


def _middleware_event_message(user_id, text):
    return SimpleNamespace(
        from_user=_tg(user_id),
        chat=SimpleNamespace(id=user_id, username=None, type="private"),
        text=text,
        answer=AsyncMock(),
    )


def _middleware_event_callback(user_id, data):
    return SimpleNamespace(
        from_user=_tg(user_id),
        data=data,
        message=SimpleNamespace(
            chat=SimpleNamespace(id=user_id, username=None, type="private")
        ),
        answer=AsyncMock(),
    )


async def test_middleware_blocks_search_for_unapproved_user_and_persists_pending(
    db, monkeypatch
):
    import bot.middlewares.db as db_middleware

    _, factory = db
    monkeypatch.setattr(db_middleware, "get_session_factory", lambda: factory)
    handler = AsyncMock(return_value="handled")
    event = _middleware_event_message(9200, "/search anatomy")

    result = await DbMiddleware()(
        handler, event, {"event_from_user": _tg(9200)}
    )

    assert result is None
    handler.assert_not_awaited()
    event.answer.assert_awaited_once()
    assert "access" in event.answer.await_args.args[0].lower()

    async with factory() as check:
        user = await check.get(User, 9200)
        assert access_status(user) == PENDING


async def test_middleware_blocks_unauthorized_watch_callback(db, monkeypatch):
    import bot.middlewares.db as db_middleware

    _, factory = db
    monkeypatch.setattr(db_middleware, "get_session_factory", lambda: factory)
    handler = AsyncMock(return_value="handled")
    event = _middleware_event_callback(9201, "watch:1")

    result = await DbMiddleware()(
        handler, event, {"event_from_user": _tg(9201)}
    )

    assert result is None
    handler.assert_not_awaited()
    event.answer.assert_awaited_once_with(
        "Library access required. Use /start to request access.",
        show_alert=True,
    )


async def test_middleware_allows_start_and_access_request(db, monkeypatch):
    import bot.middlewares.db as db_middleware

    _, factory = db
    monkeypatch.setattr(db_middleware, "get_session_factory", lambda: factory)
    handler = AsyncMock(return_value="handled")

    start_event = _middleware_event_message(9202, "/start")
    assert await DbMiddleware()(
        handler, start_event, {"event_from_user": _tg(9202)}
    ) == "handled"

    request_event = _middleware_event_callback(9202, "request_access")
    assert await DbMiddleware()(
        handler, request_event, {"event_from_user": _tg(9202)}
    ) == "handled"
    assert handler.await_count == 2


async def test_middleware_allows_approved_user_then_blocks_after_revocation(
    db, monkeypatch
):
    import bot.middlewares.db as db_middleware

    _, factory = db
    async with factory() as setup:
        user = await _add_user(setup, user_id=9203, status=APPROVED)

    monkeypatch.setattr(db_middleware, "get_session_factory", lambda: factory)
    handler = AsyncMock(return_value="handled")
    text_event = _middleware_event_message(9203, "/subjects")

    assert await DbMiddleware()(
        handler, text_event, {"event_from_user": _tg(9203)}
    ) == "handled"
    assert handler.await_count == 1

    async with factory() as update_session:
        await revoke_access(update_session, 9203, 123)

    blocked_event = _middleware_event_message(9203, "/subjects")
    assert await DbMiddleware()(
        handler, blocked_event, {"event_from_user": _tg(9203)}
    ) is None
    assert handler.await_count == 1
    blocked_event.answer.assert_awaited_once()
    async with factory() as check:
        user = await check.get(User, 9203)
        assert access_status(user) == REJECTED


async def test_middleware_blocks_expired_user_but_allows_my_access(
    db, monkeypatch
):
    import bot.middlewares.db as db_middleware

    _, factory = db
    async with factory() as setup:
        await _add_user(
            setup,
            user_id=9205,
            status=APPROVED,
            access_expires_at=datetime.utcnow() - timedelta(minutes=1),
        )

    monkeypatch.setattr(db_middleware, "get_session_factory", lambda: factory)
    handler = AsyncMock(return_value="handled")

    blocked_event = _middleware_event_message(9205, "/subjects")
    assert await DbMiddleware()(
        handler, blocked_event, {"event_from_user": _tg(9205)}
    ) is None
    assert "expired" in blocked_event.answer.await_args.args[0].lower()

    allowed_event = _middleware_event_message(9205, "🔑 My Access")
    assert await DbMiddleware()(
        handler, allowed_event, {"event_from_user": _tg(9205)}
    ) == "handled"
    handler.assert_awaited_once()


async def test_middleware_does_not_block_upload_inbox(db, monkeypatch):
    import bot.middlewares.db as db_middleware

    _, factory = db
    monkeypatch.setattr(db_middleware, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        db_middleware,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001", admin_ids={123}),
    )
    handler = AsyncMock(return_value="handled")
    event = SimpleNamespace(
        from_user=_tg(9204),
        chat=SimpleNamespace(id=-1001, username=None, type="supergroup"),
        text=None,
        answer=AsyncMock(),
    )

    assert await DbMiddleware()(
        handler, event, {"event_from_user": _tg(9204)}
    ) == "handled"
    handler.assert_awaited_once()


async def test_access_commands_reject_non_positive_user_ids(session):
    bot = AsyncMock()

    message = _message(123, "/approveuser -5")
    await approve_user_cmd(message, session, bot)

    message.answer.assert_awaited_once()
    assert "Usage" in message.answer.await_args.args[0]
    assert await session.get(User, -5) is None
    bot.send_message.assert_not_awaited()


async def test_lecture_open_and_watch_recheck_access(session):
    subject = Subject(slug="anatomy", name="Anatomy", emoji="A", order=1)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)
    lecture = Lecture(
        subject_id=subject.id,
        title="Protected Lecture",
        channel_message_id=123,
        order=1,
    )
    session.add(lecture)
    await session.commit()
    await session.refresh(lecture)
    user = await _add_user(session, user_id=9300, status=PENDING)
    bot = AsyncMock()

    open_cb = _callback(f"lec:{lecture.id}", user.id)
    await open_lecture(open_cb, session, user, bot)
    open_cb.answer.assert_awaited_once_with(
        "Library access not approved. Use /start to request access.",
        show_alert=True,
    )
    open_cb.message.edit_text.assert_not_awaited()

    watch_cb = _callback(f"watch:{lecture.id}", user.id)
    await watch_lecture(watch_cb, session, user, bot)
    watch_cb.answer.assert_awaited_once_with(
        "Library access not approved. Use /start to request access.",
        show_alert=True,
    )
    bot.send_video.assert_not_awaited()
    bot.copy_message.assert_not_awaited()
