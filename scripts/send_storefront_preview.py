"""Одноразовая отправка владельцу витрины для скриншотов платёжного сервиса."""

from __future__ import annotations

import asyncio
import sqlite3

from aiogram import Bot
from aiogram.enums import ParseMode

from avito_bot.config import Settings
from avito_bot.keyboards import plans_keyboard
from avito_bot.plans import get_plans


def owner_chat_id(settings: Settings) -> int:
    """Взять из локальной БД Telegram ID первого (владельческого) аккаунта."""

    connection = sqlite3.connect(settings.database_path)
    try:
        row = connection.execute(
            "SELECT chat_id FROM users ORDER BY created_at LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError("В базе ещё нет пользователей")
    return int(row[0])


async def main() -> None:
    """Отправить тестовую витрину и обязательно закрыть HTTP-сессию бота."""

    settings = Settings.load()
    plans = get_plans(settings)
    bot = Bot(settings.bot_token)
    try:
        await bot.send_message(
            owner_chat_id(settings),
            "<b>AvitoRacketka — мониторинг новых объявлений Avito</b>\n\n"
            "Бот проверяет сохранённые поиски и присылает новые объявления "
            "с фотографией, ценой, описанием и прямой ссылкой.\n\n"
            "<b>Тарифы на 7 дней:</b>\n"
            "• «Старт» — 2 поиска, 200 ₽ / 150 ⭐\n"
            "• «Про» — 5 поисков, 350 ₽ / 250 ⭐\n\n"
            "🎁 Первые 24 часа и один поиск — бесплатно.\n"
            "Выберите тариф для оформления заказа:",
            parse_mode=ParseMode.HTML,
            reply_markup=plans_keyboard(plans),
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
