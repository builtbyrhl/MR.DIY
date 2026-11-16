import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bot.main as main


async def test_run_polling_starts_one_poller_and_cancels_background_tasks(
    monkeypatch,
):
    settings = SimpleNamespace(
        reminder_hour_utc=14,
        digest_weekday=6,
        digest_hour_utc=14,
        bot_token_env="BRAINEXE_BOT_TOKEN",
    )
    bot = SimpleNamespace(delete_webhook=AsyncMock())
    dispatcher = SimpleNamespace(
        start_polling=AsyncMock(side_effect=asyncio.CancelledError)
    )
    real_create_task = asyncio.create_task
    created_tasks = []
    cleanup_task = real_create_task(asyncio.sleep(3600))

    def create_task(coro):
        task = real_create_task(coro)
        created_tasks.append(task)
        return task

    start_cleanup = Mock(return_value=cleanup_task)
    calls = {"sync": 0, "reminder": 0, "digest": 0}

    async def sync_catalogue():
        calls["sync"] += 1
        await asyncio.sleep(3600)

    async def reminder_loop(_bot, _hour):
        calls["reminder"] += 1
        await asyncio.sleep(3600)

    async def digest_loop(_bot, _weekday, _hour):
        calls["digest"] += 1
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_bot", lambda _: bot)
    monkeypatch.setattr(main, "build_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(
        main, "_start_health_server", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(main, "on_startup", AsyncMock())
    monkeypatch.setattr(main, "start_media_cleanup", start_cleanup)
    monkeypatch.setattr(main, "sync_catalogue_background", sync_catalogue)
    monkeypatch.setattr(main, "reminder_loop", reminder_loop)
    monkeypatch.setattr(main, "digest_loop", digest_loop)
    monkeypatch.setattr(main.asyncio, "create_task", create_task)

    with pytest.raises(asyncio.CancelledError):
        await main.run_polling()

    assert dispatcher.start_polling.await_count == 1
    assert bot.delete_webhook.await_count == 1
    assert start_cleanup.call_count == 1
    assert calls == {"sync": 1, "reminder": 1, "digest": 1}
    assert len(created_tasks) == 3
    assert cleanup_task.cancelled()
    assert all(task.cancelled() for task in created_tasks)
