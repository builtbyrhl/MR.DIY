from __future__ import annotations

from bot.config import get_settings


def is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_ids


def require_admin(user_id: int) -> None:
    if not is_admin(user_id):
        raise PermissionError("admin only")
