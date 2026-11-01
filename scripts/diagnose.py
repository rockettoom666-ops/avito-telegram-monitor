"""Быстрая проверка токена, webhook и списка команд Telegram-бота."""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from avito_bot.config import Settings


async def main() -> None:
    """Запросить у Telegram основные диагностические сведения и вывести их."""

    settings = Settings.load()
    session = AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else None
    bot = Bot(settings.bot_token, session=session)
    try:
        identity = await bot.get_me()
        webhook = await bot.get_webhook_info()
        commands = await bot.get_my_commands()
        print(
            f"username=@{identity.username} id={identity.id} "
            f"webhook={bool(webhook.url)} pending={webhook.pending_update_count} "
            f"commands={','.join(command.command for command in commands)}"
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
