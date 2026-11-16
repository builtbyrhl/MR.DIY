import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot.main as main
from bot.main import notify_error
from bot.redaction import configure_redaction, redact_secrets


SECRET_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
SECRET_DB_PASSWORD = "db-super-secret-password"
SECRET_DB_URL = (
    f"postgresql+asyncpg://app:{SECRET_DB_PASSWORD}@db.internal:5432/app"
)


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


def test_redact_secrets_preserves_useful_diagnostics():
    configure_redaction(
        extra_secrets=[SECRET_TOKEN, SECRET_DB_PASSWORD]
    )

    text = redact_secrets(
        f"token={SECRET_TOKEN} db={SECRET_DB_URL}"
    )

    assert SECRET_TOKEN not in text
    assert SECRET_DB_PASSWORD not in text
    assert "token=<redacted>" in text
    assert "db.internal" in text


def test_env_configured_secrets_are_redacted(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "rzp-secret-value")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook-secret-value")
    configure_redaction()

    text = redact_secrets(
        "RAZORPAY_KEY_SECRET=rzp-secret-value "
        "WEBHOOK_SECRET=webhook-secret-value"
    )

    assert "rzp-secret-value" not in text
    assert "webhook-secret-value" not in text
    assert "RAZORPAY_KEY_SECRET=<redacted>" in text
    assert "WEBHOOK_SECRET=<redacted>" in text


def test_logging_redacts_secrets_and_traceback(caplog):
    configure_redaction(
        extra_secrets=[SECRET_TOKEN, SECRET_DB_PASSWORD]
    )

    with caplog.at_level(logging.ERROR):
        try:
            raise ValueError(
                f"token={SECRET_TOKEN} db={SECRET_DB_URL}"
            )
        except ValueError:
            logging.getLogger("test.redaction").exception(
                "request failed"
            )

    formatted = logging.Formatter().format(caplog.records[-1])
    assert SECRET_TOKEN not in formatted
    assert SECRET_DB_PASSWORD not in formatted
    assert "ValueError" in formatted
    assert "db.internal" in formatted


async def test_error_report_redacts_secrets():
    _reset_throttle()
    configure_redaction(
        extra_secrets=[SECRET_TOKEN, SECRET_DB_PASSWORD]
    )
    event = _event(
        ValueError(
            f"token={SECRET_TOKEN} db={SECRET_DB_URL}"
        ),
        text=f"/crash {SECRET_TOKEN}",
    )

    await notify_error(event)

    _, text = event.bot.send_message.await_args.args
    assert SECRET_TOKEN not in text
    assert SECRET_DB_PASSWORD not in text
    assert "ValueError" in text
    assert "db.internal" in text
    assert "message: /crash <redacted>" in text
