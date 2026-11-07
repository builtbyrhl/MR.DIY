from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from bot.config import get_settings

logger = logging.getLogger(__name__)

ACCESS_GROUP_NAME = "Med Lounge"

INVITE_AVAILABLE = "available"
INVITE_EXPIRED = "expired"
INVITE_FAILED = "failed"
INVITE_NOT_GENERATED = "not_generated"


@dataclass(frozen=True)
class InviteResult:
    link: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class InviteState:
    status: str
    link: str | None = None
    expires_at: datetime | None = None
    error: str | None = None


_invite_states: dict[int, InviteState] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _chat_target(raw: str) -> int | str:
    value = raw.strip()
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)
    return value


def access_group_config_errors() -> list[str]:
    settings = get_settings()
    if not settings.access_group_id.strip():
        return ["ACCESS_GROUP_ID or DESTINATION_GROUP_ID"]
    return []


def invite_state(user_id: int, *, now: datetime | None = None) -> InviteState:
    state = _invite_states.get(user_id)
    if state is None:
        return InviteState(status=INVITE_NOT_GENERATED)
    if (
        state.status == INVITE_AVAILABLE
        and state.expires_at is not None
        and state.expires_at <= (now or _now())
    ):
        return InviteState(
            status=INVITE_EXPIRED,
            link=state.link,
            expires_at=state.expires_at,
        )
    return state


def clear_invite_state(user_id: int | None = None) -> None:
    if user_id is None:
        _invite_states.clear()
    else:
        _invite_states.pop(user_id, None)


async def create_access_invite(bot: Bot, user_id: int) -> InviteResult:
    settings = get_settings()
    group_id = settings.access_group_id.strip()
    if not group_id:
        error = "The private student group is not configured."
        _invite_states[user_id] = InviteState(
            status=INVITE_FAILED, error=error
        )
        return InviteResult(error=error)

    ttl = max(1, min(getattr(settings, "access_invite_ttl_minutes", 10), 60))
    expires_at = _now() + timedelta(minutes=ttl)
    expire_date = int(expires_at.timestamp())
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=_chat_target(group_id),
            member_limit=1,
            expire_date=expire_date,
            creates_join_request=False,
            name=f"u{user_id}",
        )
    except TelegramRetryAfter as exc:
        logger.warning(
            "Invite creation rate limited for user %s (retry in %ss)",
            user_id,
            exc.retry_after,
        )
        error = f"Telegram rate limited invitations. Try again in {exc.retry_after}s."
        _invite_states[user_id] = InviteState(
            status=INVITE_FAILED, error=error
        )
        return InviteResult(error=error)
    except TelegramAPIError as exc:
        logger.warning(
            "Invite creation failed for user %s: %s",
            user_id,
            type(exc).__name__,
        )
        error = "The private student group invitation could not be created."
        _invite_states[user_id] = InviteState(
            status=INVITE_FAILED, error=error
        )
        return InviteResult(error=error)
    except Exception:
        logger.warning(
            "Invite creation failed for user %s",
            user_id,
            exc_info=True,
        )
        error = "The private student group invitation could not be created."
        _invite_states[user_id] = InviteState(
            status=INVITE_FAILED, error=error
        )
        return InviteResult(error=error)

    link = getattr(invite, "invite_link", None)
    if not link:
        error = "Telegram did not return an invitation link."
        _invite_states[user_id] = InviteState(
            status=INVITE_FAILED, error=error
        )
        return InviteResult(error=error)

    _invite_states[user_id] = InviteState(
        status=INVITE_AVAILABLE,
        link=link,
        expires_at=expires_at,
    )
    return InviteResult(link=link)
