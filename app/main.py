import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiohttp import web as aioweb

from app import db, notify
from app.bot import router, set_bot_commands
from app.config import settings
from app.scheduler import build_scheduler

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


async def main() -> None:
    db.connect()
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    async def _send(text: str) -> None:
        await bot.send_message(settings.telegram_user_id, text)

    notify.set_notifier(_send)

    try:
        await set_bot_commands(bot)
    except Exception:
        log.warning("couldn't set bot commands", exc_info=True)

    scheduler = build_scheduler(bot)
    scheduler.start()

    web_runner = None
    if settings.dashboard_password:
        try:
            from app.web import create_app
            web_runner = aioweb.AppRunner(create_app())
            await web_runner.setup()
            await aioweb.TCPSite(web_runner, "0.0.0.0", settings.dashboard_port).start()
            log.info("dashboard listening on :%d", settings.dashboard_port)
        except Exception:
            # the dashboard is a convenience — never let it take the bot down
            log.exception("dashboard failed to start")
            web_runner = None

    try:
        await _send("🟢 SF journal bot is up.")
    except Exception:
        # A fresh bot can't message a user who never pressed Start on it.
        log.warning("startup message failed — open the bot in Telegram and press Start")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if web_runner is not None:
            await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
