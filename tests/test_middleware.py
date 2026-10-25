from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.db.models import AdminUser, User
from bot.middlewares import db as db_middleware
from bot.middlewares.db import DbMiddleware
from bot.middlewares.throttling import ThrottlingMiddleware
from bot.services.auth import is_admin, set_db_admin_cache


async def test_inbox_updates_bypass_throttling(monkeypatch):
    monkeypatch.setattr(
        "bot.middlewares.throttling.get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    middleware = ThrottlingMiddleware(rate=60)
    calls = 0

    async def handler(event, data):
        nonlocal calls
        calls += 1
        return "ok"

    event = SimpleNamespace(chat=SimpleNamespace(id=-1001, username=None))
    data = {"event_from_user": SimpleNamespace(id=7)}

    assert await middleware(handler, event, data) == "ok"
    assert await middleware(handler, event, data) == "ok"
    assert calls == 2


async def test_normal_messages_remain_throttled(monkeypatch):
    monkeypatch.setattr(
        "bot.middlewares.throttling.get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    middleware = ThrottlingMiddleware(rate=60)
    calls = 0

    async def handler(event, data):
        nonlocal calls
        calls += 1
        return "ok"

    event = SimpleNamespace(chat=SimpleNamespace(id=-2002, username=None))
    data = {"event_from_user": SimpleNamespace(id=8)}

    assert await middleware(handler, event, data) == "ok"
    assert await middleware(handler, event, data) is None
    assert calls == 1


async def test_whereami_command_is_never_throttled(monkeypatch):
    monkeypatch.setattr(
        "bot.middlewares.throttling.get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    middleware = ThrottlingMiddleware(rate=60)
    calls = 0

    async def handler(event, data):
        nonlocal calls
        calls += 1
        return "ok"

    event = SimpleNamespace(
        chat=SimpleNamespace(id=-2002, username=None),
        text="/whereami",
    )
    data = {"event_from_user": SimpleNamespace(id=8)}

    assert await middleware(handler, event, data) == "ok"
    assert await middleware(handler, event, data) == "ok"
    assert calls == 2


async def test_other_commands_are_still_throttled(monkeypatch):
    monkeypatch.setattr(
        "bot.middlewares.throttling.get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    middleware = ThrottlingMiddleware(rate=60)
    calls = 0

    async def handler(event, data):
        nonlocal calls
        calls += 1
        return "ok"

    event = SimpleNamespace(
        chat=SimpleNamespace(id=-2002, username=None),
        text="/review",
    )
    data = {"event_from_user": SimpleNamespace(id=8)}

    assert await middleware(handler, event, data) == "ok"
    assert await middleware(handler, event, data) is None
    assert calls == 1


def _db_event(user_id, text="/help"):
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=user_id,
            username=f"user{user_id}",
            full_name=f"User {user_id}",
        ),
        chat=SimpleNamespace(id=user_id, username=None, type="private"),
        text=text,
        answer=AsyncMock(),
    )


async def test_db_middleware_refreshes_admin_cache_from_database(
    db, monkeypatch
):
    _, factory = db
    async with factory() as setup:
        setup.add(User(id=456, full_name="DB Admin"))
        await setup.flush()
        setup.add(AdminUser(user_id=456, granted_by=123))
        await setup.commit()

    set_db_admin_cache(set())
    monkeypatch.setattr(db_middleware, "get_session_factory", lambda: factory)
    handler = AsyncMock(return_value="handled")

    first_event = _db_event(456)
    assert await DbMiddleware()(
        handler, first_event, {"event_from_user": first_event.from_user}
    ) == "handled"
    assert is_admin(456) is True

    async with factory() as setup:
        admin = await setup.get(AdminUser, 456)
        await setup.delete(admin)
        await setup.commit()

    assert is_admin(456) is True

    second_event = _db_event(456)
    assert await DbMiddleware()(
        handler, second_event, {"event_from_user": second_event.from_user}
    ) == "handled"
    assert is_admin(456) is False
    assert handler.await_count == 2
