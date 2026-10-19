from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from bot.config import get_settings
from bot.db.session import init_db
from bot.middlewares.db import DbMiddleware
from bot.middlewares.throttling import ThrottlingMiddleware

from bot.handlers import start, subjects, lectures, bookmarks, progress, search, premium, admin, channel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    # middlewares
    dp.message.middleware(DbMiddleware())
    dp.callback_query.middleware(DbMiddleware())
    dp.message.middleware(ThrottlingMiddleware())
    dp.callback_query.middleware(ThrottlingMiddleware())
    # routers
    for r in (start.router, subjects.router, lectures.router, bookmarks.router,
              progress.router, search.router, premium.router, admin.router, channel.router):
        dp.include_router(r)
    return dp


async def on_startup(bot: Bot):
    await init_db()
    logger.info("DB initialized")


async def run_polling():
    settings = get_settings()
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = build_dispatcher()
    await on_startup(bot)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])


async def run_webhook():
    settings = get_settings()
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = build_dispatcher()
    await on_startup(bot)
    await bot.set_webhook(settings.webhook_url, secret_token=settings.webhook_secret, allowed_updates=["message", "callback_query", "chat_member"])
    app = web.Application()
    async def handle_webhook(request):
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != settings.webhook_secret:
            return web.Response(status=403)
        data = await request.json()
        await dp.feed_webhook_update(bot, data)
        return web.Response(text="ok")
    app.router.add_post("/", handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)
    await site.start()
    logger.info("Webhook running on %s:%s", settings.host, settings.port)
    await asyncio.Event().wait()


def main():
    settings = get_settings()
    if settings.is_webhook:
        asyncio.run(run_webhook())
    else:
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
