from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember

from bot.db.models import User
from bot.handlers.access import (
    access_invite_cb,
    approve_user_cmd,
    my_access_view,
    request_access_cb,
    revoke_user_cmd,
)
from bot.handlers.channel import is_configured_chat
from bot.handlers.start import cb_verify_membership
from bot.services.access import APPROVED, PENDING, REJECTED, access_status
from bot.services.onboarding import (
    check_channel_membership,
    onboarding_config_errors,
)


def _settings(**overrides):
    values = {
        "onboarding_channel_id": "-1001",
        "onboarding_channel_url": "https://t.me/official",
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


@pytest.mark.parametrize(
    ("status", "is_member", "joined"),
    [
        ("creator", None, True),
        ("administrator", None, True),
        ("member", None, True),
        ("restricted", True, True),
        ("restricted", False, False),
        ("left", None, False),
        ("kicked", None, False),
    ],
)
async def test_membership_statuses(
    monkeypatch, status, is_member, joined
):
    _enable_onboarding(monkeypatch)
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(
        status=status, is_member=is_member
    )

    result = await check_channel_membership(bot, 42)

    assert result.joined is joined
    assert result.unavailable is False


async def test_unexpected_membership_status_fails_closed(monkeypatch):
    _enable_onboarding(monkeypatch)
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(status="unknown")

    result = await check_channel_membership(bot, 42)

    assert result.joined is False
    assert result.unavailable is True


async def test_membership_api_error_fails_closed(monkeypatch):
    _enable_onboarding(monkeypatch)
    bot = AsyncMock()
    bot.get_chat_member.side_effect = TelegramBadRequest(
        GetChatMember(chat_id=-1001, user_id=42), "chat not found"
    )

    result = await check_channel_membership(bot, 42)

    assert result.joined is False
    assert result.unavailable is True


async def test_verify_callback_does_not_grant_access(session, monkeypatch):
    _enable_onboarding(monkeypatch)
    user = await _add_user(session, status=PENDING)
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(status="member")
    cb = _callback("onboarding:verify", user.id)

    await cb_verify_membership(cb, user, bot)

    await session.refresh(user)
    assert access_status(user) == PENDING
    assert user.access_requested_at is None
    cb.message.edit_text.assert_awaited_once()
    cb.answer.assert_awaited_once_with("Membership verified.")


async def test_non_member_cannot_request_access(session, monkeypatch):
    _enable_onboarding(monkeypatch)
    user = await _add_user(session, status=REJECTED)
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(status="left")
    cb = _callback("request_access", user.id)

    await request_access_cb(cb, session, user, bot)

    await session.refresh(user)
    assert access_status(user) == REJECTED
    assert user.access_requested_at is None
    bot.send_message.assert_not_awaited()
    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs["show_alert"] is True


async def test_approval_creates_single_use_invite(session, monkeypatch):
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
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert _urls(markup) == ["https://t.me/+single-use"]


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
    cb.answer.assert_awaited_once_with("Invitation created.")


async def test_my_access_offers_group_invite_when_configured(
    session, monkeypatch
):
    import bot.handlers.access as access

    monkeypatch.setattr(
        access,
        "get_settings",
        lambda: SimpleNamespace(access_group_id="-1002"),
    )
    user = await _add_user(session, user_id=8804, status=APPROVED)

    text, markup = my_access_view(user)

    assert "Library access is active" in text
    assert _button_data(markup) == ["access:invite"]


async def test_revocation_does_not_remove_group_member(session):
    user = await _add_user(session, user_id=8803, status=APPROVED)
    bot = AsyncMock()

    await revoke_user_cmd(_message(123, "/revokeuser 8803"), session, bot)

    await session.refresh(user)
    assert access_status(user) == REJECTED
    bot.ban_chat_member.assert_not_awaited()
    bot.kick_chat_member.assert_not_awaited()


def test_channel_filter_only_accepts_configured_chats():
    settings = _settings(channel_id="@source")

    assert is_configured_chat(
        SimpleNamespace(id=-1001, username=None), settings
    )
    assert is_configured_chat(
        SimpleNamespace(id=-1002, username=None), settings
    )
    assert is_configured_chat(
        SimpleNamespace(id=-2000, username="source"), settings
    )
    assert not is_configured_chat(
        SimpleNamespace(id=-2001, username="other"), settings
    )


def test_onboarding_config_errors_reports_missing(monkeypatch):
    import bot.services.onboarding as onboarding

    monkeypatch.setattr(
        onboarding,
        "get_settings",
        lambda: _settings(
            onboarding_channel_id="",
            onboarding_channel_url="",
            access_group_id="",
        ),
    )

    assert onboarding_config_errors() == [
        "ONBOARDING_CHANNEL_ID",
        "ONBOARDING_CHANNEL_URL",
        "ACCESS_GROUP_ID or DESTINATION_GROUP_ID",
    ]
