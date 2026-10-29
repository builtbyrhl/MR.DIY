from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from bot.config import get_settings
from bot.db.session import get_session_factory, init_db
from bot.middlewares.db import DbMiddleware
from bot.middlewares.throttling import ThrottlingMiddleware
from bot.services.auth import load_db_admin_cache
from bot.services.catalog import sync_published_uploads_to_catalog
from bot.services.media_delivery import start_media_cleanup
from bot.services.reminders import digest_loop, reminder_loop

from bot.handlers import start, subjects, lectures, bookmarks, progress, search, premium, access, admin, channel, review, ingest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    # middlewares
    dp.message.middleware(DbMiddleware())
    dp.edited_message.middleware(DbMiddleware())
    dp.callback_query.middleware(DbMiddleware())
    dp.message.middleware(ThrottlingMiddleware())
    dp.edited_message.middleware(ThrottlingMiddleware())
    dp.callback_query.middleware(ThrottlingMiddleware())
    # routers
    for r in (start.router, subjects.router, lectures.router, bookmarks.router,
              progress.router, search.router, premium.router, access.router,
              admin.router, channel.router, review.router, ingest.router):
        dp.include_router(r)
    return dp


async def on_startup(bot: Bot):
    try:
        await init_db()
        logger.info("DB initialized")
    except Exception:
        logger.exception("Failed during init_db")
        raise
    try:
        async with get_session_factory()() as session:
            admin_ids = await load_db_admin_cache(session)
    except Exception:
        logger.exception("Failed to load admin cache")
        raise
    logger.info("Admin cache loaded (%s database admins)", len(admin_ids))


async def sync_catalogue_background() -> None:
    """Project published uploads into the student catalogue in the background.

    Runs after polling has begun so a deploy or restart does not leave the
    bot silent while the initial sync works through historical uploads.
    """
    try:
        async with get_session_factory()() as session:
            created = await sync_published_uploads_to_catalog(session)
        logger.info("Background catalogue sync complete (%s new lectures)", created)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Background catalogue sync failed; lectures from earlier "
            "publications may be missing until the next successful sync"
        )


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("Background task ended with an error during shutdown", exc_info=True)


async def _start_health_server(settings) -> web.AppRunner | None:
    """Bind the host health port before slow startup work begins.

    Hosts such as Northflank restart a pod when its health check cannot
    connect. Starting the server first means database migrations and the
    initial catalogue sync cannot cause a health-check restart loop.
    """
    if not settings.port:
        return None
    app = web.Application()

    async def health(request):
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)
    await site.start()
    logger.info("Health server running on %s:%s", settings.host, settings.port)
    return runner


async def run_polling():
    settings = get_settings()
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = build_dispatcher()
    runner = await _start_health_server(settings)
    cleanup_task = None
    sync_task = None
    reminder_task = None
    digest_task = None
    try:
        await on_startup(bot)
        # Keep pending updates: messages sent while the bot was down are
        # delivered when polling resumes instead of being discarded.
        await bot.delete_webhook(drop_pending_updates=False)
        cleanup_task = start_media_cleanup(bot)
        sync_task = asyncio.create_task(sync_catalogue_background())
        reminder_task = asyncio.create_task(
            reminder_loop(bot, settings.reminder_hour_utc)
        )
        digest_task = asyncio.create_task(
            digest_loop(bot, settings.digest_weekday, settings.digest_hour_utc)
        )
        try:
            await dp.start_polling(bot, allowed_updates=["message", "edited_message", "callback_query", "chat_member"], drop_pending_updates=False)
        except TelegramConflictError:
            logger.error(
                "Telegram polling conflict: another instance is polling with this "
                "BOT_TOKEN. Stop the duplicate deployment/replica, then restart."
            )
            raise
    finally:
        await _cancel_task(cleanup_task)
        await _cancel_task(sync_task)
        await _cancel_task(reminder_task)
        await _cancel_task(digest_task)
        if runner is not None:
            await runner.cleanup()


async def run_webhook():
    settings = get_settings()
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = build_dispatcher()

    app = web.Application()

    async def handle_webhook(request):
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != settings.webhook_secret:
            return web.Response(status=403)
        data = await request.json()
        await dp.feed_webhook_update(bot, data)
        return web.Response(text="ok")

    async def health(request):
        return web.Response(text="ok")

    app.router.add_post("/", handle_webhook)
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)
    await site.start()
    logger.info("Webhook server listening on %s:%s", settings.host, settings.port)

    cleanup_task = None
    sync_task = None
    reminder_task = None
    digest_task = None
    try:
        await on_startup(bot)
        await bot.set_webhook(settings.webhook_url, secret_token=settings.webhook_secret, allowed_updates=["message", "edited_message", "callback_query", "chat_member"])
        cleanup_task = start_media_cleanup(bot)
        sync_task = asyncio.create_task(sync_catalogue_background())
        reminder_task = asyncio.create_task(
            reminder_loop(bot, settings.reminder_hour_utc)
        )
        digest_task = asyncio.create_task(
            digest_loop(bot, settings.digest_weekday, settings.digest_hour_utc)
        )
        await asyncio.Event().wait()
    finally:
        await _cancel_task(cleanup_task)
        await _cancel_task(sync_task)
        await _cancel_task(reminder_task)
        await _cancel_task(digest_task)
        await runner.cleanup()


def main():
    try:
        settings = get_settings()
    except Exception as exc:
        logging.getLogger(__name__).error("Invalid configuration: %s", exc)
        raise SystemExit(1)
    if not settings.bot_token:
        logging.getLogger(__name__).error("BOT_TOKEN is required")
        raise SystemExit(1)
    if settings.is_webhook:
        asyncio.run(run_webhook())
    else:
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
