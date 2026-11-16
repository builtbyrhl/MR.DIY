from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.config import get_settings
from bot.db.models import User
from bot.handlers.channel import dm_welcome_text
from bot.handlers.start import build_home, cmd_start, help_text
from bot.services.access import APPROVED


IDENTITIES = [
    (
        "mrdiy",
        "MR.DIY",
        "Your organised medical lecture library.",
        "🎓",
        "Brain.exe",
    ),
    (
        "brainexe",
        "Brain.exe",
        "Your brain's favourite operating system.",
        "🧠",
        "MR.DIY",
    ),
]


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _use_identity(monkeypatch, identity):
    monkeypatch.setenv("BOT_IDENTITY", identity)
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("BRAINEXE_BOT_TOKEN", "test-brain-token")
    get_settings.cache_clear()
    return get_settings()


def _message(user_id=9001):
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=user_id,
            username=f"user{user_id}",
            full_name=f"User {user_id}",
        ),
        chat=SimpleNamespace(id=user_id, username=None, type="private"),
        text="",
        answer=AsyncMock(),
    )


async def _add_user(session, status, user_id=9001):
    user = User(
        id=user_id,
        username=f"user{user_id}",
        full_name=f"User {user_id}",
        is_premium=False,
        is_admin=False,
        access_status=status,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.mark.parametrize(
    ("identity", "name", "tagline", "emoji", "other_name"),
    IDENTITIES,
)
async def test_onboarding_branding_matches_identity(
    session, monkeypatch, identity, name, tagline, emoji, other_name
):
    _use_identity(monkeypatch, identity)
    user = await _add_user(session, status=None)
    message = _message()

    await cmd_start(message, session, user)

    text = message.answer.await_args.args[0]
    assert f"{emoji} Welcome to *{name}*" in text
    assert tagline in text
    assert other_name not in text
    assert "Med Lounge" in text
    assert "No other channel is required" in text


@pytest.mark.parametrize(
    ("identity", "name", "tagline", "emoji", "other_name"),
    IDENTITIES,
)
async def test_home_branding_matches_identity(
    session, monkeypatch, identity, name, tagline, emoji, other_name
):
    _use_identity(monkeypatch, identity)
    user = await _add_user(session, status=APPROVED)

    text, _ = await build_home(session, user)

    assert f"{emoji} *{name}*" in text
    assert tagline in text
    assert other_name not in text


@pytest.mark.parametrize(
    ("identity", "name", "tagline", "emoji", "other_name"),
    IDENTITIES,
)
def test_help_and_channel_welcome_use_identity_branding(
    monkeypatch, identity, name, tagline, emoji, other_name
):
    settings = _use_identity(monkeypatch, identity)
    user = SimpleNamespace(full_name="Ada Lovelace", first_name="Ada")

    assert f"*{name} Help*" in help_text(False)
    assert f"*{name} Help*" in help_text(True)
    assert "/admin" not in help_text(False)
    assert "/admin" in help_text(True)

    text = dm_welcome_text(settings, user)
    assert f"{emoji} {name} is online" in text
    assert tagline in text
    assert other_name not in text
