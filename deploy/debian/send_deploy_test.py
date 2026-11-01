"""Отправить владельцу одно сообщение для проверки Debian-развёртывания."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from dotenv import dotenv_values


async def send() -> None:
    """Прочитать закрытый env и проверить доставку Telegram через VPN."""

    values = dotenv_values(Path("/etc/avito-bot.env"))
    token = (values.get("TELEGRAM_BOT_TOKEN") or "").strip()
    proxy = (values.get("PROXY_URL") or "").strip()
    admin_ids = [
        int(value.strip())
        for value in (values.get("ADMIN_CHAT_IDS") or "").split(",")
        if value.strip()
    ]
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    if not admin_ids:
        raise RuntimeError("ADMIN_CHAT_IDS не содержит получателя")

    session = AiohttpSession(proxy=proxy) if proxy else AiohttpSession()
    bot = Bot(token=token, session=session)
    try:
        await bot.send_message(
            admin_ids[0],
            (
                "✅ Бот перенесён на Debian и запущен.\n"
                "VPN, Telegram, Chromium и автозапуск проверены."
            ),
        )
    finally:
        await bot.session.close()

    print("telegram_deploy_message=sent")


if __name__ == "__main__":
    asyncio.run(send())
