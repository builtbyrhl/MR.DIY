from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db.models import AdminUser


_db_admin_ids: set[int] = set()


def owner_ids() -> set[int]:
    """Return the immutable owner/superadmin IDs from ``ADMIN_IDS``."""
    return set(get_settings().admin_ids)


def db_admin_ids() -> set[int]:
    return set(_db_admin_ids)


def all_admin_ids() -> set[int]:
    return owner_ids() | db_admin_ids()


def is_owner(user_id: int) -> bool:
    return user_id in owner_ids()


def is_admin(user_id: int) -> bool:
    return user_id in all_admin_ids()


def set_db_admin_cache(admin_ids: set[int]) -> None:
    global _db_admin_ids
    _db_admin_ids = set(admin_ids)


async def load_db_admin_cache(session: AsyncSession) -> set[int]:
    """Load database-granted admins into the process-local authorization cache."""
    result = await session.execute(select(AdminUser.user_id))
    admin_ids = {int(user_id) for user_id in result.scalars().all()}
    set_db_admin_cache(admin_ids)
    return admin_ids


def require_admin(user_id: int) -> None:
    if not is_admin(user_id):
        raise PermissionError("admin only")
