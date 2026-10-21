from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.handlers.admin import whereami_cmd


def _message(*, user_id, chat_id, chat_title, chat_type="supergroup", thread_id=None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=chat_id, title=chat_title, type=chat_type),
        message_thread_id=thread_id,
        answer=AsyncMock(),
    )


async def test_whereami_reports_chat_id_title_and_no_thread():
    message = _message(
        user_id=123,
        chat_id=-1001234567890,
        chat_title="Upload Inbox",
    )

    await whereami_cmd(message, )

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "chat.id: -1001234567890" in text
    assert "message_thread_id: None" in text
    assert "chat title: Upload Inbox" in text


async def test_whereami_reports_forum_topic_id():
    message = _message(
        user_id=123,
        chat_id=-1001234567890,
        chat_title="Upload Inbox",
        thread_id=42,
    )

    await whereami_cmd(message, )

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "chat.id: -1001234567890" in text
    assert "message_thread_id: 42" in text
    assert "chat title: Upload Inbox" in text


async def test_whereami_falls_back_to_chat_type_without_title():
    message = _message(
        user_id=123,
        chat_id=123456789,
        chat_title=None,
        chat_type="private",
    )

    await whereami_cmd(message, )

    text = message.answer.await_args.args[0]
    assert "chat.id: 123456789" in text
    assert "chat title: private" in text


async def test_whereami_is_admin_only():
    message = _message(
        user_id=999,
        chat_id=-1001234567890,
        chat_title="Upload Inbox",
    )

    await whereami_cmd(message, )

    message.answer.assert_not_awaited()
