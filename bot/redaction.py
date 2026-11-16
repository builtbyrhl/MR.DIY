from __future__ import annotations

import logging
import os
import re
import threading
import traceback
from typing import TYPE_CHECKING, Iterable
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from bot.config import Settings


REDACTED = "<redacted>"
_SECRET_ENV_NAMES = (
    "BOT_TOKEN",
    "BRAINEXE_BOT_TOKEN",
    "RAZORPAY_KEY_SECRET",
    "WEBHOOK_SECRET",
    "DATABASE_URL",
    "REDIS_URL",
)
_PLACEHOLDER_SECRETS = {
    "",
    "secret",
    "changeme",
    "change-me",
    "password",
    "postgres",
    "redis",
}
_TELEGRAM_TOKEN_RE = re.compile(
    r"(?<!\w)\d{6,12}:[A-Za-z0-9_-]{30,}(?!\w)"
)
_URL_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"token|secret|password|passwd|pwd|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"private[_-]?key"
    r")\b(\s*[=:]\s*)([^\s,;&]+)"
)
_ENV_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)[A-Z0-9_]*)"
    r"\s*=\s*([^\s,;&]+)"
)

_secrets: set[str] = set()
_secrets_lock = threading.Lock()
_log_factory_installed = False


def _env_secrets() -> set[str]:
    values = {
        value.strip()
        for name in _SECRET_ENV_NAMES
        if (value := os.getenv(name))
    }
    return {value for value in values if value not in _PLACEHOLDER_SECRETS}


def _url_password(value: str) -> str | None:
    try:
        password = urlsplit(value).password
    except ValueError:
        return None
    return unquote(password) if password else None


def _settings_secrets(settings: Settings) -> set[str]:
    values = {
        settings.mrdiy_bot_token,
        settings.brainexe_bot_token,
        settings.razorpay_key_secret,
        settings.webhook_secret,
        settings.database_url,
        settings.redis_url,
    }
    values.update(
        password
        for url in (settings.database_url, settings.redis_url)
        if (password := _url_password(url))
    )
    return {
        value.strip()
        for value in values
        if value and value.strip() not in _PLACEHOLDER_SECRETS
    }


def configure_redaction(
    settings: Settings | None = None,
    extra_secrets: Iterable[str] = (),
) -> None:
    values = _env_secrets()
    if settings is not None:
        values.update(_settings_secrets(settings))
    values.update(
        value.strip()
        for value in extra_secrets
        if value and value.strip() not in _PLACEHOLDER_SECRETS
    )
    with _secrets_lock:
        _secrets.update(values)


def redact_secrets(value: object) -> str:
    text = str(value)
    if not text:
        return text

    with _secrets_lock:
        _secrets.update(_env_secrets())
        secrets = sorted(
            (secret for secret in _secrets if secret),
            key=len,
            reverse=True,
        )

    for secret in secrets:
        text = text.replace(secret, REDACTED)
    text = _TELEGRAM_TOKEN_RE.sub(REDACTED, text)
    text = _URL_CREDENTIAL_RE.sub(
        lambda match: f"{match.group('scheme')}{REDACTED}@",
        text,
    )
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",
        text,
    )
    text = _ENV_SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}={REDACTED}",
        text,
    )
    return text


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    try:
        message = record.getMessage()
    except Exception:
        message = str(record.msg)
    record.msg = redact_secrets(message)
    record.args = ()
    if record.exc_info:
        record.exc_text = redact_secrets(
            "".join(traceback.format_exception(*record.exc_info))
        )
    elif record.exc_text:
        record.exc_text = redact_secrets(record.exc_text)
    if record.stack_info:
        record.stack_info = redact_secrets(record.stack_info)
    return record


def install_logging_redaction() -> None:
    global _log_factory_installed
    with _secrets_lock:
        if _log_factory_installed:
            return
        previous_factory = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            return _redact_record(previous_factory(*args, **kwargs))

        logging.setLogRecordFactory(factory)
        _log_factory_installed = True
