import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from bot.handlers.admin import whereami_cmd

# Structurally valid placeholder token (aiogram validates the format); not a real credential.
FAKE_TOKEN = ":".join(["123456789", "A" * 35])


def _message(*, user_id, chat_id, chat_title, chat_type="supergroup", thread_id=None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=chat_id, title=chat_title, type=chat_type),
        message_thread_id=thread_id,
        message_id=7,
        answer=AsyncMock(),
    )


class _CapturingBot(Bot):
    def __init__(self, token: str = FAKE_TOKEN):
        super().__init__(token=token)
        self.sent = []

    async def __call__(self, method, request_timeout=None, **kwargs):
        self.sent.append(method)
        return None


def _real_message(*, chat, thread_id=None, is_topic_message=False, text="/whereami", user_id=123):
    return Message(
        message_id=11,
        date=datetime.datetime.now(),
        chat=chat,
        from_user=User(id=user_id, is_bot=False, first_name="Admin"),
        text=text,
        message_thread_id=thread_id,
        is_topic_message=is_topic_message,
    )


@pytest.fixture(scope="module")
def dispatcher():
    from bot.main import build_dispatcher

    return build_dispatcher()


async def _feed(dispatcher, message, update_id=1):
    bot = _CapturingBot()
    await dispatcher.feed_update(bot, Update(update_id=update_id, message=message))
    return bot.sent


async def test_whereami_reports_chat_id_title_and_no_thread():
    message = _message(
        user_id=123,
        chat_id=123456,
        chat_title=None,
        chat_type="private",
    )

    await whereami_cmd(message)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "chat.id: 123456" in text
    assert "message_thread_id: None" in text
    assert "chat title: private" in text


async def test_whereami_reports_forum_topic_id():
    message = _message(
        user_id=123,
        chat_id=-1001234567890,
        chat_title="Upload Inbox",
        thread_id=42,
    )

    await whereami_cmd(message)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "chat.id: -1001234567890" in text
    assert "message_thread_id: 42" in text
    assert "chat title: Upload Inbox" in text


async def test_whereami_replies_to_the_command_message():
    message = _message(
        user_id=123,
        chat_id=-1001234567890,
        chat_title="Upload Inbox",
        thread_id=42,
    )

    await whereami_cmd(message)

    reply_parameters = message.answer.await_args.kwargs["reply_parameters"]
    assert reply_parameters.message_id == message.message_id


async def test_whereami_is_admin_only():
    message = _message(
        user_id=999,
        chat_id=-1001234567890,
        chat_title="Upload Inbox",
    )

    await whereami_cmd(message)

    message.answer.assert_not_awaited()


async def test_whereami_ignores_message_without_sender():
    message = _message(
        user_id=123,
        chat_id=-1001234567890,
        chat_title="Upload Inbox",
    )
    message.from_user = None

    await whereami_cmd(message)

    message.answer.assert_not_awaited()


async def test_whereami_is_reachable_in_forum_topic(dispatcher, db, monkeypatch):
    import bot.middlewares.db as db_middleware

    _, factory = db
    monkeypatch.setattr(db_middleware, "get_session_factory", lambda: factory)

    chat = Chat(id=-1001234567890, type="supergroup", title="Upload Inbox")
    sent = await _feed(
        dispatcher,
        _real_message(chat=chat, thread_id=42, is_topic_message=True),
    )

    assert len(sent) == 1
    reply = sent[0]
    assert reply.chat_id == chat.id
    assert "chat.id: -1001234567890" in reply.text
    assert "message_thread_id: 42" in reply.text
    assert "chat title: Upload Inbox" in reply.text
    assert reply.message_thread_id == 42
    assert reply.reply_parameters is not None
    assert reply.reply_parameters.message_id == 11


async def test_whereami_is_reachable_in_private_chat(dispatcher, db, monkeypatch):
    import bot.middlewares.db as db_middleware

    _, factory = db
    monkeypatch.setattr(db_middleware, "get_session_factory", lambda: factory)

    chat = Chat(id=123456, type="private", first_name="Admin")
    sent = await _feed(dispatcher, _real_message(chat=chat))

    assert len(sent) == 1
    assert "chat.id: 123456" in sent[0].text
    assert "message_thread_id: None" in sent[0].text


async def test_whereami_answers_twice_in_a_row(dispatcher, db, monkeypatch):
    import bot.middlewares.db as db_middleware

    _, factory = db
    monkeypatch.setattr(db_middleware, "get_session_factory", lambda: factory)

    chat = Chat(id=-1001234567890, type="supergroup", title="Upload Inbox")
    first = await _feed(
        dispatcher,
        _real_message(chat=chat, thread_id=42, is_topic_message=True),
        update_id=1,
    )
    second = await _feed(
        dispatcher,
        _real_message(chat=chat, thread_id=42, is_topic_message=True),
        update_id=2,
    )

    assert len(first) == 1
    assert len(second) == 1
