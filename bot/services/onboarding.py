from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from bot.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MembershipCheck:
    joined: bool
    unavailable: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class InviteResult:
    link: str | None = None
    error: str | None = None


def _chat_target(raw: str) -> int | str:
    value = raw.strip()
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)
    return value


def onboarding_config_errors() -> list[str]:
    settings = get_settings()
    missing = []
    if not settings.onboarding_channel_id.strip():
        missing.append("ONBOARDING_CHANNEL_ID")
    if not settings.onboarding_channel_url.strip():
        missing.append("ONBOARDING_CHANNEL_URL")
    if not settings.access_group_id.strip():
        missing.append("ACCESS_GROUP_ID or DESTINATION_GROUP_ID")
    return missing


def onboarding_channel_url() -> str:
    return get_settings().onboarding_channel_url.strip()


def _status_value(member) -> str | None:
    status = getattr(member, "status", None)
    value = getattr(status, "value", status)
    return value if isinstance(value, str) else None


async def check_channel_membership(bot: Bot, user_id: int) -> MembershipCheck:
    settings = get_settings()
    channel_id = settings.onboarding_channel_id.strip()
    if not channel_id:
        return MembershipCheck(
            joined=False,
            unavailable=True,
            reason="The official channel is not configured.",
        )
    try:
        member = await bot.get_chat_member(
            chat_id=_chat_target(channel_id), user_id=user_id
        )
    except TelegramRetryAfter as exc:
        logger.warning(
            "Channel membership check rate limited for user %s (retry in %ss)",
            user_id,
            exc.retry_after,
        )
        return MembershipCheck(
            joined=False,
            unavailable=True,
            reason=f"Telegram rate limited the check. Try again in {exc.retry_after}s.",
        )
    except TelegramAPIError as exc:
        logger.warning(
            "Channel membership check failed for user %s: %s",
            user_id,
            type(exc).__name__,
        )
        return MembershipCheck(
            joined=False,
            unavailable=True,
            reason="Telegram could not verify membership right now.",
        )
    except Exception:
        logger.warning(
            "Channel membership check failed for user %s",
            user_id,
            exc_info=True,
        )
        return MembershipCheck(
            joined=False,
            unavailable=True,
            reason="Telegram could not verify membership right now.",
        )

    status = _status_value(member)
    if status in {
        ChatMemberStatus.CREATOR.value,
        ChatMemberStatus.ADMINISTRATOR.value,
        ChatMemberStatus.MEMBER.value,
    }:
        return MembershipCheck(joined=True)
    if status == ChatMemberStatus.RESTRICTED.value:
        if bool(getattr(member, "is_member", False)):
            return MembershipCheck(joined=True)
        return MembershipCheck(
            joined=False,
            reason="Your account is restricted in the official channel.",
        )
    if status in {ChatMemberStatus.LEFT.value, ChatMemberStatus.KICKED.value}:
        return MembershipCheck(
            joined=False,
            reason="Join the official channel to continue.",
        )
    return MembershipCheck(
        joined=False,
        unavailable=True,
        reason="Telegram returned an unexpected membership status.",
    )


async def create_access_invite(bot: Bot, user_id: int) -> InviteResult:
    settings = get_settings()
    group_id = settings.access_group_id.strip()
    if not group_id:
        return InviteResult(
            error="The private group is not configured.",
        )
    ttl = max(1, min(getattr(settings, "access_invite_ttl_minutes", 10), 60))
    expire_date = int(
        (
            datetime.now(timezone.utc) + timedelta(minutes=ttl)
        ).timestamp()
    )
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
        return InviteResult(
            error=f"Telegram rate limited invitations. Try again in {exc.retry_after}s.",
        )
    except TelegramAPIError as exc:
        logger.warning(
            "Invite creation failed for user %s: %s",
            user_id,
            type(exc).__name__,
        )
        return InviteResult(
            error="The private group invitation could not be created.",
        )
    except Exception:
        logger.warning(
            "Invite creation failed for user %s",
            user_id,
            exc_info=True,
        )
        return InviteResult(
            error="The private group invitation could not be created.",
        )

    link = getattr(invite, "invite_link", None)
    if not link:
        return InviteResult(
            error="Telegram did not return an invitation link.",
        )
    return InviteResult(link=link)
