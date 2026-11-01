"""Планировщик проверок Avito и надёжной доставки уведомлений."""

from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from datetime import timedelta

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile

from avito_bot.avito import AvitoBlockedError, AvitoRateLimitError, AvitoScraper
from avito_bot.db import Database
from avito_bot.models import Listing, Subscription
from avito_bot.utils import format_listing_caption, format_listing_message, utc_now

logger = logging.getLogger(__name__)


class MonitorService:
    """Связывает базу данных, Avito-парсер и Telegram.

    Сервис состоит из двух независимых циклов:
    1. ``run`` проверяет поиски и складывает новые объявления в outbox.
    2. ``run_outbox`` отправляет накопленные сообщения и повторяет ошибки.
    """

    def __init__(self, db: Database, scraper: AvitoScraper, bot: Bot):
        self.db = db
        self.scraper = scraper
        self.bot = bot
        self._wake_event = asyncio.Event()

    def wake(self) -> None:
        """Разбудить планировщик после команды «проверить сейчас» или оплаты."""

        self._wake_event.set()

    async def run(self) -> None:
        """Постоянно проверять подписки, срок которых уже наступил."""

        while True:
            try:
                await self.check_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected monitor loop error")
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=20)
                self._wake_event.clear()
            except TimeoutError:
                pass

    async def run_outbox(self) -> None:
        """Отправлять подготовленные уведомления и отмечать результат в БД."""

        while True:
            try:
                messages = await self.db.pending_outbox()
                for message in messages:
                    try:
                        text = format_listing_message(
                            message.title,
                            message.price,
                            message.description,
                            message.url,
                        )
                        # Сначала пытаемся отправить фото с подписью. Если CDN
                        # Avito не отдаёт изображение, пользователь всё равно
                        # получит полноценное текстовое уведомление.
                        if message.photo_url:
                            try:
                                photo_bytes = await self.scraper.download_photo(
                                    message.photo_url
                                )
                                await self.bot.send_photo(
                                    message.chat_id,
                                    photo=BufferedInputFile(
                                        photo_bytes,
                                        filename=f"avito-{message.item_id}.jpg",
                                    ),
                                    caption=format_listing_caption(
                                        message.title,
                                        message.price,
                                        message.description,
                                        message.url,
                                    ),
                                    parse_mode=ParseMode.HTML,
                                )
                            except asyncio.CancelledError:
                                raise
                            except Exception as photo_error:
                                logger.warning(
                                    "Photo delivery failed for outbox %s, "
                                    "using text fallback: %s",
                                    message.id,
                                    photo_error,
                                )
                                await self.bot.send_message(
                                    message.chat_id,
                                    text,
                                    parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=False,
                                )
                        else:
                            await self.bot.send_message(
                                message.chat_id,
                                text,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=False,
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        # Сообщение остаётся в outbox. База назначит следующую
                        # попытку с увеличивающейся задержкой.
                        attempts = message.attempts + 1
                        logger.warning(
                            "Outbox %s delivery failed: %s", message.id, error
                        )
                        await self.db.mark_outbox_failed(
                            message.id, attempts, str(error)
                        )
                    else:
                        await self.db.mark_outbox_sent(message.id)
                await asyncio.sleep(5 if messages else 10)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected outbox loop error")
                await asyncio.sleep(10)

    async def check_due(self) -> None:
        """Загрузить все поиски, для которых наступило время проверки."""

        due = await self.db.due_subscriptions()
        # Один и тот же URL может принадлежать нескольким пользователям. Avito
        # загружается один раз, а результат затем обрабатывается для каждого.
        grouped: dict[str, list[Subscription]] = defaultdict(list)
        for subscription in due:
            grouped[subscription.url].append(subscription)
        for url, subscriptions in grouped.items():
            detail_cache: dict[str, Listing] = {}
            try:
                listings = await self.scraper.fetch_search(url)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                for subscription in subscriptions:
                    await self._record_failure(subscription, error)
                continue

            for subscription in subscriptions:
                try:
                    await self.process_snapshot(
                        subscription, listings, detail_cache=detail_cache
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.exception(
                        "Failed processing subscription %s", subscription.id
                    )
                    await self._record_failure(subscription, error)

    async def process_snapshot(
        self,
        subscription: Subscription,
        listings: list[Listing],
        *,
        detail_cache: dict[str, Listing] | None = None,
    ) -> None:
        """Сравнить свежую выдачу с уже известными объявлениями подписки."""

        if subscription.initialized_at is None:
            # Ключевое правило: первая выдача только запоминается. Без этого при
            # создании поиска бот отправил бы пользователю десятки старых товаров.
            await self.db.initialize_baseline(subscription.id, listings)
            await self._record_success(subscription)
            logger.info(
                "Initialized subscription %s with %d baseline items",
                subscription.id,
                len(listings),
            )
            return

        unseen_ids = await self.db.unseen_item_ids(subscription.id, listings)
        for listing in listings:
            if listing.item_id not in unseen_ids:
                continue
            cache = detail_cache if detail_cache is not None else {}
            # Полная карточка открывается только для действительно нового ID.
            # Кэш не даёт повторно открывать её для одинаковых URL подписок.
            detailed = cache.get(listing.item_id)
            if detailed is None and listing.detail_complete:
                detailed = listing
                cache[listing.item_id] = detailed
            elif detailed is None:
                detailed = await self.scraper.fetch_detail(listing)
                cache[listing.item_id] = detailed
            if self._is_old_resurfaced(subscription, detailed):
                await self.db.record_listing_and_enqueue(
                    subscription, detailed, baseline=True
                )
                logger.info(
                    "Ignored old resurfaced item %s for subscription %s",
                    detailed.item_id,
                    subscription.id,
                )
                continue
            await self.db.record_listing_and_enqueue(subscription, detailed)
        await self._record_success(subscription)

    @staticmethod
    def _is_old_resurfaced(subscription: Subscription, listing: Listing) -> bool:
        """Отсеять старое объявление, которое продавец просто поднял в выдаче."""

        if listing.published_at is None or subscription.initialized_at is None:
            return False
        grace = timedelta(minutes=5)
        return listing.published_at < subscription.initialized_at - grace

    async def _record_success(self, subscription: Subscription) -> None:
        """Запланировать обычную следующую проверку со случайным сдвигом."""

        jitter = random.randint(-15, 15)
        next_check = utc_now() + timedelta(
            minutes=subscription.interval_minutes, seconds=jitter
        )
        await self.db.mark_check_success(subscription.id, next_check)

    async def _record_failure(
        self, subscription: Subscription, error: Exception
    ) -> None:
        """Записать ошибку и назначить повтор без часовой паузы при блокировке."""

        if isinstance(error, AvitoBlockedError):
            # И 429, и капча повторяются через обычный интервал поиска.
            # Ранее обычная капча попадала в общий backoff вплоть до 60 минут.
            backoff_minutes = subscription.interval_minutes
        else:
            backoff_minutes = min(
                60,
                max(
                    subscription.interval_minutes,
                    5 * (2 ** min(subscription.error_count, 4)),
                ),
            )
        next_check = utc_now() + timedelta(minutes=backoff_minutes)
        await self.db.mark_check_failure(subscription.id, str(error), next_check)
        logger.warning(
            "Subscription %s failed; retry in %d minutes: %s",
            subscription.id,
            backoff_minutes,
            error,
        )
        if isinstance(error, AvitoBlockedError):
            should_notify = await self.db.claim_blocked_notice(subscription.id)
            if should_notify:
                if isinstance(error, AvitoRateLimitError):
                    action = (
                        "Avito временно ограничил текущий выходной IP (ошибка "
                        "429). Не запускайте "
                        "частые ручные проверки — бот повторит запрос после "
                        "паузы."
                    )
                elif self.scraper.manual_window_opened:
                    action = (
                        "Окно Chromium уже развёрнуто на экране ноутбука — "
                        "капча запущена; пройдите её вручную."
                    )
                else:
                    action = (
                        "Сейчас рабочий стол ноутбука не активен. Войдите в "
                        "графическую сессию пользователя rockettoom: при "
                        "следующей попытке бот сам откроет окно Chromium и "
                        "попробует запустить капчу."
                    )
                await self.bot.send_message(
                    subscription.chat_id,
                    "⚠️ Avito запросил проверку браузера. "
                    f"{action} "
                    "Бот продолжит попытки автоматически; следующая — примерно через "
                    f"{backoff_minutes} мин.",
                )
