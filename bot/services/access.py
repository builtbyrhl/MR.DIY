from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db.models import User
from bot.services.auth import all_admin_ids, is_admin

logger = logging.getLogger(__name__)

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
VALID_ACCESS_STATUSES = frozenset({PENDING, APPROVED, REJECTED})


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def access_status(user: User) -> str:
    status = getattr(user, "access_status", None)
    return status if status in VALID_ACCESS_STATUSES else PENDING


def access_expires_at(user: User) -> Optional[datetime]:
    value = getattr(user, "access_expires_at", None)
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def access_is_expired(
    user: User, *, now: Optional[datetime] = None
) -> bool:
    expires = access_expires_at(user)
    return expires is not None and expires <= (now or utcnow())


def format_access_expiry(value: Optional[datetime]) -> str:
    if value is None:
        return "No expiry"
    return value.strftime("%d %b %Y, %H:%M UTC")


def has_library_access(user: User) -> bool:
    return is_admin(user.id) or (
        access_status(user) == APPROVED and not access_is_expired(user)
    )


def display_name(user: User) -> str:
    if user.username:
        return f"@{user.username} ({user.id})"
    if user.full_name:
        return f"{user.full_name} ({user.id})"
    return str(user.id)


async def request_access(session: AsyncSession, user_id: int) -> bool:
    """Move a non-approved user to pending atomically.

    Returns True only when this call created the pending request, which makes
    repeated and concurrent button presses safe to handle idempotently.
    """
    now = utcnow()
    expired_approval = and_(
        User.access_status == APPROVED,
        User.access_expires_at.is_not(None),
        User.access_expires_at <= now,
    )
    result = await session.execute(
        update(User)
        .where(
            User.id == user_id,
            or_(
                User.access_status.is_(None),
                User.access_status.notin_((APPROVED, PENDING)),
                expired_approval,
            ),
        )
        .values(
            access_status=PENDING,
            access_requested_at=now,
            access_decided_at=None,
            access_decided_by=None,
            access_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


async def approve_access(
    session: AsyncSession,
    user_id: int,
    admin_id: int,
    *,
    create_missing: bool = False,
    days: Optional[int] = None,
) -> Optional[User]:
    user = await session.get(User, user_id)
    if user is None:
        if not create_missing:
            return None
        user = User(id=user_id, is_admin=is_admin(user_id))
        session.add(user)

    now = utcnow()
    duration = get_settings().access_default_days if days is None else days
    if user.access_requested_at is None:
        user.access_requested_at = now
    user.access_status = APPROVED
    user.access_decided_at = now
    user.access_decided_by = admin_id
    user.access_expires_at = (
        now + timedelta(days=duration) if duration and duration > 0 else None
    )
    await session.commit()
    await session.refresh(user)
    return user


async def reject_access(
    session: AsyncSession, user_id: int, admin_id: int
) -> Optional[User]:
    user = await session.get(User, user_id)
    if user is None:
        return None
    user.access_status = REJECTED
    user.access_decided_at = utcnow()
    user.access_decided_by = admin_id
    user.access_expires_at = None
    await session.commit()
    await session.refresh(user)
    return user


async def revoke_access(
    session: AsyncSession, user_id: int, admin_id: int
) -> Optional[User]:
    return await reject_access(session, user_id, admin_id)


async def list_pending(session: AsyncSession, limit: int = 20) -> list[User]:
    result = await session.execute(
        select(User)
        .where(User.access_status == PENDING)
        .order_by(User.access_requested_at, User.id)
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_expiring_access(
    session: AsyncSession, *, within_days: int = 7, limit: int = 20
) -> list[User]:
    now = utcnow()
    result = await session.execute(
        select(User)
        .where(
            User.access_status == APPROVED,
            User.access_expires_at.is_not(None),
            User.access_expires_at > now,
            User.access_expires_at <= now + timedelta(days=within_days),
        )
        .order_by(User.access_expires_at, User.id)
        .limit(limit)
    )
    return list(result.scalars().all())


async def notify_admins(bot, text: str, reply_markup=None) -> None:
    for admin_id in sorted(all_admin_ids()):
        try:
            await bot.send_message(
                admin_id,
                text,
                reply_markup=reply_markup,
                parse_mode=None,
            )
        except Exception:
            logger.warning("Could not notify admin %s", admin_id, exc_info=True)
