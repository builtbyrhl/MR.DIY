from types import SimpleNamespace

from bot.middlewares.throttling import ThrottlingMiddleware


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
