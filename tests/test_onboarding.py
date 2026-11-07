from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.db.models import User
from bot.handlers.access import (
    access_invite_cb,
    approve_user_cmd,
    my_access_view,
    request_access_cb,
    revoke_user_cmd,
)
from bot.handlers.channel import is_configured_chat
from bot.handlers.start import cmd_start
from bot.services.access import APPROVED, PENDING, REJECTED, access_status
from bot.services.onboarding import (
    ACCESS_GROUP_NAME,
    INVITE_AVAILABLE,
    INVITE_EXPIRED,
    INVITE_FAILED,
    INVITE_NOT_GENERATED,
    InviteState,
    access_group_config_errors,
    clear_invite_state,
    invite_state,
)


def _settings(**overrides):
    values = {
        "channel_id": "@source",
        "access_group_id": "-1002",
        "access_invite_ttl_minutes": 10,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _enable_onboarding(monkeypatch, **overrides):
    import bot.services.onboarding as onboarding

    settings = _settings(**overrides)
    monkeypatch.setattr(onboarding, "get_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def _reset_invite_state():
    clear_invite_state()
    yield
    clear_invite_state()


def _tg(user_id=9001):
    return SimpleNamespace(
        id=user_id,
        username=f"user{user_id}",
        full_name=f"User {user_id}",
    )


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
        if button.callback_data
    ]


def _button_labels(markup):
    return [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]


def _urls(markup):
    return [
        button.url
        for row in markup.inline_keyboard
        for button in row
        if button.url
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


def test_access_group_config_errors_reports_only_group(monkeypatch):
    import bot.services.onboarding as onboarding

    monkeypatch.setattr(
        onboarding,
        "get_settings",
        lambda: _settings(access_group_id=""),
    )

    assert access_group_config_errors() == [
        "ACCESS_GROUP_ID or DESTINATION_GROUP_ID",
    ]


async def test_no_channel_configuration_required_for_start(
    session, monkeypatch
):
    _enable_onboarding(monkeypatch)
    user = await _add_user(session, status=REJECTED)
    message = _message(user.id)

    await cmd_start(message, session, user)

    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _button_data(markup) == ["request_access"]
    assert ACCESS_GROUP_NAME in text
    assert "No other channel is required" in text
    assert "not approved" in text.lower()


async def test_request_access_does_not_check_channel_membership(
    session, monkeypatch
):
    _enable_onboarding(monkeypatch)
    user = await _add_user(session, status=REJECTED)
    bot = AsyncMock()
    cb = _callback("request_access", user.id)

    await request_access_cb(cb, session, user, bot)

    await session.refresh(user)
    assert access_status(user) == PENDING
    assert user.access_requested_at is not None
    bot.get_chat_member.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    cb.answer.assert_awaited_once_with("Request sent")


async def test_approval_creates_single_use_med_lounge_invite(
    session, monkeypatch
):
    _enable_onboarding(monkeypatch)
    bot = AsyncMock()
    bot.create_chat_invite_link.return_value = SimpleNamespace(
        invite_link="https://t.me/+single-use"
    )

    await approve_user_cmd(_message(123, "/approveuser 8800"), session, bot)

    user = await session.get(User, 8800)
    assert access_status(user) == APPROVED
    kwargs = bot.create_chat_invite_link.await_args.kwargs
    assert kwargs["chat_id"] == -1002
    assert kwargs["member_limit"] == 1
    assert kwargs["creates_join_request"] is False
    assert kwargs["name"] == "u8800"
    assert kwargs["expire_date"] > int(
        (datetime.now(timezone.utc) + timedelta(minutes=9)).timestamp()
    )
    message_text = bot.send_message.await_args.args[1]
    assert ACCESS_GROUP_NAME in message_text
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert _urls(markup) == ["https://t.me/+single-use"]
    assert invite_state(8800).status == INVITE_AVAILABLE


async def test_invite_failure_keeps_approval_and_offers_retry(
    session, monkeypatch
):
    _enable_onboarding(monkeypatch)
    bot = AsyncMock()
    bot.create_chat_invite_link.side_effect = RuntimeError("telegram down")

    await approve_user_cmd(_message(123, "/approveuser 8801"), session, bot)

    user = await session.get(User, 8801)
    assert access_status(user) == APPROVED
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert _button_data(markup) == ["access:invite"]
    assert invite_state(8801).status == INVITE_FAILED


async def test_approved_user_can_retry_invite(session, monkeypatch):
    _enable_onboarding(monkeypatch)
    user = await _add_user(session, user_id=8802, status=APPROVED)
    bot = AsyncMock()
    bot.create_chat_invite_link.return_value = SimpleNamespace(
        invite_link="https://t.me/+retry"
    )
    cb = _callback("access:invite", user.id)

    await access_invite_cb(cb, user, bot)

    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert _urls(markup) == ["https://t.me/+retry"]
    cb.answer.assert_awaited_once_with(
        f"{ACCESS_GROUP_NAME} invitation created."
    )
    assert invite_state(user.id).status == INVITE_AVAILABLE


async def test_my_access_reports_available_invitation(session, monkeypatch):
    import bot.handlers.access as access

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    monkeypatch.setattr(
        access,
        "get_settings",
        lambda: _settings(access_group_id="-1002"),
    )
    monkeypatch.setattr(
        access,
        "invite_state",
        lambda user_id: InviteState(
            status=INVITE_AVAILABLE,
            link="https://t.me/+available",
            expires_at=expires_at,
        ),
    )
    user = await _add_user(session, user_id=8804, status=APPROVED)

    text, markup = my_access_view(user)

    assert "Library access is active" in text
    assert "invitation is available" in text
    assert ACCESS_GROUP_NAME in text
    assert _urls(markup) == ["https://t.me/+available"]


async def test_my_access_reports_expired_invitation(session, monkeypatch):
    import bot.handlers.access as access

    monkeypatch.setattr(
        access,
        "get_settings",
        lambda: _settings(access_group_id="-1002"),
    )
    monkeypatch.setattr(
        access,
        "invite_state",
        lambda user_id: InviteState(status=INVITE_EXPIRED),
    )
    user = await _add_user(session, user_id=8805, status=APPROVED)

    text, markup = my_access_view(user)

    assert "expired" in text.lower()
    assert _button_data(markup) == ["access:invite"]
    assert any("Regenerate" in label for label in _button_labels(markup))


async def test_my_access_reports_failed_invitation(session, monkeypatch):
    import bot.handlers.access as access

    monkeypatch.setattr(
        access,
        "get_settings",
        lambda: _settings(access_group_id="-1002"),
    )
    monkeypatch.setattr(
        access,
        "invite_state",
        lambda user_id: InviteState(
            status=INVITE_FAILED, error="telegram down"
        ),
    )
    user = await _add_user(session, user_id=8806, status=APPROVED)

    text, markup = my_access_view(user)

    assert "attempt failed" in text.lower()
    assert _button_data(markup) == ["access:invite"]
    assert any("Regenerate" in label for label in _button_labels(markup))


async def test_my_access_reports_missing_invitation_needs_generation(session, monkeypatch):
    import bot.handlers.access as access

    monkeypatch.setattr(
        access,
        "get_settings",
        lambda: _settings(access_group_id="-1002"),
    )
    monkeypatch.setattr(
        access,
        "invite_state",
        lambda user_id: InviteState(status=INVITE_NOT_GENERATED),
    )
    user = await _add_user(session, user_id=8807, status=APPROVED)

    text, markup = my_access_view(user)

    assert "No active invitation is recorded" in text
    assert _button_data(markup) == ["access:invite"]
    assert any("Get Med Lounge invite" in label for label in _button_labels(markup))


def test_invite_state_marks_expired_invitations():
    import bot.services.onboarding as onboarding

    onboarding._invite_states[1] = InviteState(
        status=INVITE_AVAILABLE,
        link="https://t.me/+expired",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    assert invite_state(1).status == INVITE_EXPIRED


async def test_revocation_does_not_remove_group_member(session):
    user = await _add_user(session, user_id=8803, status=APPROVED)
    bot = AsyncMock()

    await revoke_user_cmd(_message(123, "/revokeuser 8803"), session, bot)

    await session.refresh(user)
    assert access_status(user) == REJECTED
    bot.ban_chat_member.assert_not_awaited()
    bot.kick_chat_member.assert_not_awaited()


def test_channel_filter_only_accepts_source_and_access_group():
    settings = _settings(channel_id="@source", access_group_id="-1002")

    assert is_configured_chat(
        SimpleNamespace(id=-1002, username=None), settings
    )
    assert is_configured_chat(
        SimpleNamespace(id=-2000, username="source"), settings
    )
    assert not is_configured_chat(
        SimpleNamespace(id=-1001, username=None), settings
    )
    assert not is_configured_chat(
        SimpleNamespace(id=-2001, username="other"), settings
    )
