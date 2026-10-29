from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot.main as main
from bot.main import notify_error


def _event(exception, text="/crash"):
    return SimpleNamespace(
        exception=exception,
        update=SimpleNamespace(
            message=SimpleNamespace(text=text), callback_query=None
        ),
        bot=AsyncMock(),
    )


def _reset_throttle():
    main._error_notify_last.clear()


async def test_error_is_reported_to_admins():
    _reset_throttle()
    event = _event(ValueError("boom"))

    await notify_error(event)

    event.bot.send_message.assert_awaited_once()
    chat_id, text = event.bot.send_message.await_args.args
    assert chat_id == 123
    assert "Bot error" in text
    assert "ValueError: boom" in text
    assert "message: /crash" in text
    assert event.bot.send_message.await_args.kwargs["parse_mode"] is None


async def test_duplicate_errors_are_throttled():
    _reset_throttle()
    await notify_error(_event(ValueError("boom")))
    event = _event(ValueError("boom"))
    await notify_error(event)
    event.bot.send_message.assert_not_awaited()


async def test_not_modified_errors_stay_silent():
    _reset_throttle()
    from aiogram.exceptions import TelegramBadRequest

    event = _event(
        TelegramBadRequest(method=None, message="Bad Request: message is not modified")
    )
    await notify_error(event)
    event.bot.send_message.assert_not_awaited()
