"""Точка сборки и запуска всех компонентов Telegram-бота."""

from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from avito_bot.avito import AvitoScraper
from avito_bot.config import Settings
from avito_bot.db import Database
from avito_bot.handlers import create_router
from avito_bot.monitor import MonitorService


def configure_logging(settings: Settings) -> None:
    """Писать сообщения одновременно в консоль и ротационный файл.

    Когда bot.log достигает 5 МБ, создаётся новый файл. Хранятся три старые
    копии, поэтому логи не смогут бесконечно занимать место на диске.
    """

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        settings.log_path,
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler],
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def main() -> None:
    """Создать зависимости, запустить фоновые задачи и начать polling Telegram."""

    settings = Settings.load()
    settings.ensure_directories()
    configure_logging(settings)
    logger = logging.getLogger(__name__)

    # Все объекты создаются здесь и затем явно передаются друг другу. Благодаря
    # этому обработчики, монитор и парсер можно проверять по отдельности в тестах.
    db = Database(
        settings.database_path,
        admin_search_limit=settings.max_subscriptions_per_user,
    )
    await db.connect()
    # ID из ADMIN_CHAT_IDS получают постоянный максимальный доступ без
    # фиктивного платежа. Список заново синхронизируется при каждом запуске.
    await db.sync_admin_access(settings.admin_chat_ids)
    # PROXY_URL используется только Telegram. Для Avito существует отдельная
    # настройка AVITO_PROXY_URL; пустое значение означает прямое соединение.
    telegram_session = (
        AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else None
    )
    bot = Bot(settings.bot_token, session=telegram_session)
    scraper = AvitoScraper(
        settings.avito_profile_dir,
        headless=settings.avito_headless,
        max_items=settings.avito_max_items,
        proxy_url=settings.avito_proxy_url,
    )
    monitor = MonitorService(db, scraper, bot)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(create_router(db, settings, monitor))
    # Монитор ищет новые объявления, а outbox независимо доставляет уже
    # подготовленные уведомления. Разделение защищает от потери сообщений.
    monitor_task = asyncio.create_task(monitor.run(), name="avito-monitor")
    outbox_task = asyncio.create_task(monitor.run_outbox(), name="telegram-outbox")

    try:
        commands = [
            BotCommand(command="start", description="Начать работу"),
            BotCommand(command="id", description="Мой Telegram ID"),
            BotCommand(command="list", description="Мои поиски"),
            BotCommand(command="status", description="Состояние проверок"),
            BotCommand(command="subscription", description="Подписка и тарифы"),
            BotCommand(command="buy", description="Купить подписку"),
            BotCommand(command="faq", description="Частые вопросы"),
            BotCommand(command="terms", description="Условия использования"),
            BotCommand(command="paysupport", description="Помощь с оплатой"),
            BotCommand(command="help", description="Справка"),
        ]
        try:
            await asyncio.wait_for(bot.set_my_commands(commands), timeout=15)
        except Exception as error:
            logger.warning("Could not update Telegram command menu: %s", error)
        try:
            identity = await asyncio.wait_for(bot.get_me(), timeout=15)
            logger.info("Starting bot @%s", identity.username)
        except Exception as error:
            logger.warning("Could not read bot identity before polling: %s", error)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        # Этот блок выполняется и при обычной остановке, и при исключении:
        # фоновые циклы отменяются, затем закрываются браузер, БД и HTTP-сессия.
        for task in (monitor_task, outbox_task):
            task.cancel()
        await asyncio.gather(monitor_task, outbox_task, return_exceptions=True)
        await scraper.close()
        await db.close()
        await bot.session.close()


def run() -> None:
    """Синхронная обёртка для запуска асинхронного приложения из BAT-файла."""

    asyncio.run(main())


if __name__ == "__main__":
    run()
