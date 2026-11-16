import logging
from dataclasses import fields

import pytest

import bot.main as main
from bot.config import (
    BRAINEXE_TOKEN_ENV,
    MRDIY_TOKEN_ENV,
    ConfigurationError,
    Settings,
    get_settings,
)

SECRET_MRDIY = "123456:MRDIY-secret-token"
SECRET_BRAIN = "987654:Brain-secret-token"


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_mrdiy_is_default_and_uses_bot_token(monkeypatch):
    monkeypatch.delenv("BOT_IDENTITY", raising=False)
    monkeypatch.setenv("BOT_TOKEN", SECRET_MRDIY)
    monkeypatch.delenv("BRAINEXE_BOT_TOKEN", raising=False)

    settings = Settings()

    assert settings.bot_identity == "mrdiy"
    assert settings.bot_token_env == MRDIY_TOKEN_ENV
    assert settings.bot_token == SECRET_MRDIY
    assert settings.token_configured is True
    assert "profile=mrdiy" in settings.bot_identity_summary
    assert "display_name=MR.DIY" in settings.bot_identity_summary
    assert "token_source=BOT_TOKEN" in settings.bot_identity_summary
    assert "token_configured=yes" in settings.bot_identity_summary
    assert SECRET_MRDIY not in settings.bot_identity_summary


def test_mrdiy_identity_explicitly_uses_bot_token(monkeypatch):
    monkeypatch.setenv("BOT_IDENTITY", "mrdiy")
    monkeypatch.setenv("BOT_TOKEN", SECRET_MRDIY)
    monkeypatch.delenv("BRAINEXE_BOT_TOKEN", raising=False)

    settings = Settings()

    assert settings.bot_identity == "mrdiy"
    assert settings.bot_token_env == MRDIY_TOKEN_ENV
    assert settings.bot_token == SECRET_MRDIY


def test_brainexe_uses_only_brainexe_token(monkeypatch):
    monkeypatch.setenv("BOT_IDENTITY", "brainexe")
    monkeypatch.setenv("BOT_TOKEN", SECRET_MRDIY)
    monkeypatch.setenv("BRAINEXE_BOT_TOKEN", SECRET_BRAIN)

    settings = Settings()

    assert settings.bot_identity == "brainexe"
    assert settings.bot_token_env == BRAINEXE_TOKEN_ENV
    assert settings.bot_token == SECRET_BRAIN
    assert SECRET_MRDIY not in settings.bot_token
    assert "display_name=Brain.exe" in settings.bot_identity_summary
    assert SECRET_BRAIN not in settings.bot_identity_summary


@pytest.mark.parametrize(
    ("identity", "display_name", "tagline", "emoji"),
    [
        (
            "mrdiy",
            "MR.DIY",
            "Your organised medical lecture library.",
            "🎓",
        ),
        (
            "brainexe",
            "Brain.exe",
            "Your brain's favourite operating system.",
            "🧠",
        ),
    ],
)
def test_identity_branding(
    monkeypatch, identity, display_name, tagline, emoji
):
    monkeypatch.setenv("BOT_IDENTITY", identity)
    monkeypatch.setenv("BOT_TOKEN", SECRET_MRDIY)
    monkeypatch.setenv("BRAINEXE_BOT_TOKEN", SECRET_BRAIN)

    settings = Settings()

    assert settings.bot_display_name == display_name
    assert settings.bot_tagline == tagline
    assert settings.bot_brand_emoji == emoji


def test_identity_switch_preserves_shared_settings(monkeypatch):
    shared_env = {
        "DATABASE_URL": "postgresql+asyncpg://app:pass@db.example:5432/app",
        "REDIS_URL": "redis://cache.example:6379/1",
        "ADMIN_IDS": "101,202",
        "CHANNEL_ID": "-1001001",
        "UPLOAD_INBOX_CHAT_ID": "-1002002",
        "DESTINATION_GROUP_ID": "-1003003",
        "ACCESS_GROUP_ID": "-1004004",
        "ACCESS_INVITE_TTL_MINUTES": "15",
        "WEBHOOK_URL": "https://example.test/webhook",
        "WEBHOOK_SECRET": "webhook-secret-value",
        "HOST": "127.0.0.1",
        "PORT": "9090",
        "PREMIUM_PRICE_INR": "599",
        "RAZORPAY_KEY_ID": "rzp_test_key",
        "RAZORPAY_KEY_SECRET": "rzp-test-secret",
        "FREE_LECTURES_PER_DAY": "5",
        "ACCESS_DEFAULT_DAYS": "7",
        "MEDIA_RETENTION_HOURS": "12",
        "MEDIA_CLEANUP_INTERVAL_SECONDS": "120",
        "REMINDER_HOUR_UTC": "9",
        "DIGEST_WEEKDAY": "2",
        "DIGEST_HOUR_UTC": "10",
    }
    for name, value in shared_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BOT_TOKEN", SECRET_MRDIY)
    monkeypatch.setenv("BRAINEXE_BOT_TOKEN", SECRET_BRAIN)

    monkeypatch.setenv("BOT_IDENTITY", "mrdiy")
    mrdiy_settings = Settings()
    monkeypatch.setenv("BOT_IDENTITY", "brainexe")
    brainexe_settings = Settings()

    identity_fields = {
        "bot_identity",
        "mrdiy_bot_token",
        "brainexe_bot_token",
    }
    shared_fields = [
        field.name
        for field in fields(Settings)
        if field.name not in identity_fields
    ]
    for name in shared_fields:
        assert getattr(brainexe_settings, name) == getattr(
            mrdiy_settings, name
        ), name


def test_brainexe_missing_token_fails_without_fallback(monkeypatch):
    monkeypatch.setenv("BOT_IDENTITY", "brainexe")
    monkeypatch.setenv("BOT_TOKEN", SECRET_MRDIY)
    monkeypatch.delenv("BRAINEXE_BOT_TOKEN", raising=False)

    with pytest.raises(ConfigurationError) as excinfo:
        Settings()

    message = str(excinfo.value)
    assert "BRAINEXE_BOT_TOKEN" in message
    assert "BOT_IDENTITY=brainexe" in message
    assert SECRET_MRDIY not in message


@pytest.mark.parametrize("identity", ["other", "brain.exe", "mrdiy2"])
def test_invalid_bot_identity_is_rejected(monkeypatch, identity):
    monkeypatch.setenv("BOT_IDENTITY", identity)
    monkeypatch.setenv("BOT_TOKEN", SECRET_MRDIY)

    with pytest.raises(ConfigurationError, match="Invalid BOT_IDENTITY") as excinfo:
        Settings()

    assert SECRET_MRDIY not in str(excinfo.value)
    assert "mrdiy" in str(excinfo.value)
    assert "brainexe" in str(excinfo.value)


def test_mrdiy_missing_token_fails_clearly(monkeypatch):
    monkeypatch.setenv("BOT_IDENTITY", "mrdiy")
    monkeypatch.delenv("BOT_TOKEN", raising=False)

    with pytest.raises(ConfigurationError) as excinfo:
        Settings()

    message = str(excinfo.value)
    assert "BOT_TOKEN" in message
    assert "BOT_IDENTITY=mrdiy" in message


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ("mrdiy", SECRET_MRDIY),
        ("brainexe", SECRET_BRAIN),
    ],
)
def test_build_bot_uses_selected_token(monkeypatch, identity, expected):
    captured = {}

    class FakeBot:
        def __init__(self, token, default):
            captured["token"] = token
            captured["default"] = default

    monkeypatch.setattr(main, "Bot", FakeBot)
    settings = Settings(
        bot_identity=identity,
        mrdiy_bot_token=SECRET_MRDIY,
        brainexe_bot_token=SECRET_BRAIN,
    )

    bot = main.build_bot(settings)

    assert isinstance(bot, FakeBot)
    assert captured["token"] == expected


@pytest.mark.parametrize(
    ("identity", "display_name", "token_env"),
    [
        ("mrdiy", "MR.DIY", "BOT_TOKEN"),
        ("brainexe", "Brain.exe", "BRAINEXE_BOT_TOKEN"),
    ],
)
def test_main_logs_profile_without_tokens(
    monkeypatch, caplog, identity, display_name, token_env
):
    monkeypatch.setenv("BOT_IDENTITY", identity)
    monkeypatch.setenv("BOT_TOKEN", SECRET_MRDIY)
    monkeypatch.setenv("BRAINEXE_BOT_TOKEN", SECRET_BRAIN)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)

    async def fake_run_polling():
        return None

    monkeypatch.setattr(main, "run_polling", fake_run_polling)

    with caplog.at_level(logging.INFO):
        main.main()

    assert (
        f"Bot profile selected: profile={identity} "
        f"display_name={display_name}"
    ) in caplog.text
    assert f"token_source={token_env}" in caplog.text
    assert "token_configured=yes" in caplog.text
    assert SECRET_BRAIN not in caplog.text
    assert SECRET_MRDIY not in caplog.text


def test_main_missing_brainexe_token_logs_safe_error(monkeypatch, caplog):
    monkeypatch.setenv("BOT_IDENTITY", "brainexe")
    monkeypatch.setenv("BOT_TOKEN", SECRET_MRDIY)
    monkeypatch.delenv("BRAINEXE_BOT_TOKEN", raising=False)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as excinfo:
            main.main()

    assert excinfo.value.code == 1
    assert "BRAINEXE_BOT_TOKEN" in caplog.text
    assert "BOT_IDENTITY=brainexe" in caplog.text
    assert SECRET_MRDIY not in caplog.text


def test_main_generic_config_error_does_not_log_secret(monkeypatch, caplog):
    def broken_settings():
        raise RuntimeError(SECRET_MRDIY)

    monkeypatch.setattr(main, "get_settings", broken_settings)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as excinfo:
            main.main()

    assert excinfo.value.code == 1
    assert "RuntimeError" in caplog.text
    assert SECRET_MRDIY not in caplog.text


def test_brainexe_startup_selects_token_and_hides_it(monkeypatch, caplog):
    monkeypatch.setenv("BOT_IDENTITY", "brainexe")
    monkeypatch.setenv("BOT_TOKEN", SECRET_MRDIY)
    monkeypatch.setenv("BRAINEXE_BOT_TOKEN", SECRET_BRAIN)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)

    settings = Settings()

    assert settings.bot_token == SECRET_BRAIN
    assert settings.bot_token != SECRET_MRDIY
    assert SECRET_BRAIN not in settings.bot_identity_summary
    assert SECRET_MRDIY not in settings.bot_identity_summary

    async def fake_run_polling():
        return None

    monkeypatch.setattr(main, "run_polling", fake_run_polling)

    with caplog.at_level(logging.INFO):
        main.main()

    assert "profile=brainexe" in caplog.text
    assert "display_name=Brain.exe" in caplog.text
    assert "token_source=BRAINEXE_BOT_TOKEN" in caplog.text
    assert SECRET_BRAIN not in caplog.text
    assert SECRET_MRDIY not in caplog.text
