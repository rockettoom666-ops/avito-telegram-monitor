"""Асинхронный слой SQLite: пользователи, поиски, история, оплата и outbox."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from avito_bot.models import AccessStatus, Listing, OutboxMessage, Subscription
from avito_bot.utils import utc_now

# База разделена на пять сущностей:
# users — пробный/платный доступ; subscriptions — поиски; seen_ads — уже
# замеченные ID; outbox — очередь Telegram; payments — защита от повторного
# начисления одной и той же оплаты.
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    trial_started_at TEXT NOT NULL,
    trial_ends_at TEXT NOT NULL,
    plan_code TEXT,
    search_limit INTEGER,
    subscription_ends_at TEXT,
    lifetime_access INTEGER NOT NULL DEFAULT 0,
    admin_access INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    initialized_at TEXT,
    next_check_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_error TEXT,
    error_count INTEGER NOT NULL DEFAULT 0,
    blocked_notice_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(chat_id, url)
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_due
ON subscriptions(active, next_check_at);

CREATE TABLE IF NOT EXISTS seen_ads (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    published_at TEXT,
    baseline INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(subscription_id, item_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    title TEXT NOT NULL,
    price TEXT NOT NULL,
    description TEXT NOT NULL,
    url TEXT NOT NULL,
    photo_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    UNIQUE(subscription_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
ON outbox(sent_at, next_attempt_at);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    provider_payment_id TEXT NOT NULL,
    plan_code TEXT NOT NULL,
    search_limit INTEGER NOT NULL,
    duration_days INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    invoice_payload TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    paid_at TEXT,
    UNIQUE(provider, provider_payment_id)
);

CREATE INDEX IF NOT EXISTS idx_payments_user
ON payments(chat_id, created_at);

"""


def _dt(value: str | None) -> datetime | None:
    """Преобразовать ISO-строку SQLite обратно в datetime."""

    return datetime.fromisoformat(value) if value else None


def _subscription(row: aiosqlite.Row) -> Subscription:
    """Собрать типизированную подписку из строки результата SQLite."""

    return Subscription(
        id=row["id"],
        chat_id=row["chat_id"],
        url=row["url"],
        interval_minutes=row["interval_minutes"],
        active=bool(row["active"]),
        initialized_at=_dt(row["initialized_at"]),
        next_check_at=_dt(row["next_check_at"]) or utc_now(),
        last_checked_at=_dt(row["last_checked_at"]),
        last_error=row["last_error"],
        error_count=row["error_count"],
        blocked_notice_at=_dt(row["blocked_notice_at"]),
        created_at=_dt(row["created_at"]) or utc_now(),
    )


class Database:
    """Единственная точка доступа к SQLite для остальных модулей.

    Для операций из нескольких SQL-запросов используется lock: так два
    асинхронных обработчика не смогут одновременно создать дубль или дважды
    начислить одну оплату.
    """

    def __init__(self, path: Path, *, admin_search_limit: int = 10):
        self.path = path
        self.admin_search_limit = max(1, admin_search_limit)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        """Вернуть открытое соединение или явно сообщить об ошибке запуска."""

        if self._connection is None:
            raise RuntimeError("Database is not connected")
        return self._connection

    async def connect(self) -> None:
        """Открыть БД, создать таблицы и выполнить безопасные мини-миграции."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.executescript(SCHEMA)
        # Эти проверки позволяют запускать новую версию поверх старого файла БД
        # без ручного удаления пользовательских подписок и истории.
        user_columns = await self._connection.execute("PRAGMA table_info(users)")
        user_column_names = {row["name"] for row in await user_columns.fetchall()}
        if "search_limit" not in user_column_names:
            await self._connection.execute(
                "ALTER TABLE users ADD COLUMN search_limit INTEGER"
            )
        if "lifetime_access" not in user_column_names:
            await self._connection.execute(
                "ALTER TABLE users "
                "ADD COLUMN lifetime_access INTEGER NOT NULL DEFAULT 0"
            )
        if "admin_access" not in user_column_names:
            await self._connection.execute(
                "ALTER TABLE users "
                "ADD COLUMN admin_access INTEGER NOT NULL DEFAULT 0"
            )
        columns = await self._connection.execute("PRAGMA table_info(outbox)")
        column_names = {row["name"] for row in await columns.fetchall()}
        if "photo_url" not in column_names:
            await self._connection.execute(
                "ALTER TABLE outbox ADD COLUMN photo_url TEXT NOT NULL DEFAULT ''"
            )
        await self._connection.execute(
            """
            UPDATE outbox SET photo_url = ''
            WHERE lower(photo_url) LIKE '%/icons/%'
               OR lower(photo_url) LIKE '%touch-icon%'
               OR lower(photo_url) LIKE '%favicon%'
               OR lower(photo_url) LIKE '%placeholder%'
               OR lower(photo_url) LIKE '%no-photo%'
            """
        )
        now = utc_now()
        await self._connection.execute(
            """
            INSERT OR IGNORE INTO users
                (chat_id, trial_started_at, trial_ends_at, created_at, updated_at)
            SELECT DISTINCT chat_id, ?, ?, ?, ? FROM subscriptions
            """,
            (
                now.isoformat(),
                (now + timedelta(days=1)).isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        await self._connection.commit()

    async def close(self) -> None:
        """Закрыть соединение при остановке приложения."""

        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def ensure_user(self, chat_id: int) -> AccessStatus:
        """Создать пользователя при первом обращении и выдать 24 часа trial."""

        now = utc_now()
        await self.connection.execute(
            """
            INSERT OR IGNORE INTO users
                (chat_id, trial_started_at, trial_ends_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                now.isoformat(),
                (now + timedelta(days=1)).isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        await self.connection.commit()
        return await self.get_access(chat_id, create=False)

    async def sync_admin_access(self, chat_ids: frozenset[int]) -> None:
        """Выдать вечный полный доступ ID из ``ADMIN_CHAT_IDS``.

        Список синхронизируется при каждом запуске: добавленный ID сразу
        становится администратором, а удалённый теряет только админскую льготу,
        сохраняя собственный пробный или оплаченный тариф.
        """

        normalized_ids = sorted(set(chat_ids))
        for chat_id in normalized_ids:
            await self.ensure_user(chat_id)

        now = utc_now().isoformat()
        async with self._lock:
            await self.connection.execute(
                """
                UPDATE users
                SET admin_access = 0, updated_at = ?
                WHERE admin_access != 0
                """,
                (now,),
            )
            if normalized_ids:
                await self.connection.executemany(
                    """
                    UPDATE users
                    SET admin_access = 1, updated_at = ?
                    WHERE chat_id = ?
                    """,
                    [(now, chat_id) for chat_id in normalized_ids],
                )
            await self.connection.commit()

    async def get_access(self, chat_id: int, *, create: bool = True) -> AccessStatus:
        """Рассчитать актуальный доступ: платный, пробный или закончившийся."""

        cursor = await self.connection.execute(
            "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            if not create:
                raise RuntimeError(f"User {chat_id} was not created")
            return await self.ensure_user(chat_id)

        now = utc_now()
        paid_until = _dt(row["subscription_ends_at"])
        trial_until = _dt(row["trial_ends_at"]) or now
        # Админский доступ имеет приоритет над trial и любыми тестовыми
        # платежами, поэтому случайная покупка не уменьшит его лимит.
        if bool(row["admin_access"]):
            return AccessStatus(
                chat_id=chat_id,
                active=True,
                is_trial=False,
                is_lifetime=True,
                plan_code="admin",
                search_limit=self.admin_search_limit,
                access_until=None,
                min_interval_minutes=2,
            )
        if paid_until and paid_until > now:
            return AccessStatus(
                chat_id=chat_id,
                active=True,
                is_trial=False,
                is_lifetime=False,
                plan_code=row["plan_code"],
                search_limit=row["search_limit"] or 1,
                access_until=paid_until,
                min_interval_minutes=2,
            )
        # Бессрочный тариф остаётся базовым доступом. Если пользователь поверх
        # него купил недельный тариф, до его окончания сработала ветка выше.
        if bool(row["lifetime_access"]):
            return AccessStatus(
                chat_id=chat_id,
                active=True,
                is_trial=False,
                is_lifetime=True,
                plan_code="lifetime",
                search_limit=1,
                access_until=None,
                min_interval_minutes=15,
            )
        if trial_until > now:
            return AccessStatus(
                chat_id=chat_id,
                active=True,
                is_trial=True,
                is_lifetime=False,
                plan_code=None,
                search_limit=1,
                access_until=trial_until,
                min_interval_minutes=2,
            )
        return AccessStatus(
            chat_id=chat_id,
            active=False,
            is_trial=False,
            is_lifetime=False,
            plan_code=row["plan_code"],
            search_limit=0,
            access_until=paid_until or trial_until,
            min_interval_minutes=2,
        )

    async def activate_plan(
        self,
        *,
        chat_id: int,
        plan_code: str,
        search_limit: int,
        duration_days: int,
        lifetime: bool = False,
        provider: str,
        provider_payment_id: str,
        amount: int,
        currency: str,
        invoice_payload: str,
    ) -> tuple[AccessStatus, bool]:
        """Учесть успешный платёж и продлить подписку.

        Уникальный ID провайдера делает метод идемпотентным: повторное событие
        successful_payment не продлит доступ во второй раз.
        """

        await self.ensure_user(chat_id)
        now = utc_now()
        async with self._lock:
            cursor = await self.connection.execute(
                """
                INSERT OR IGNORE INTO payments
                    (chat_id, provider, provider_payment_id, plan_code,
                     search_limit, duration_days, amount, currency,
                     invoice_payload, status, created_at, paid_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'paid', ?, ?)
                """,
                (
                    chat_id,
                    provider,
                    provider_payment_id,
                    plan_code,
                    search_limit,
                    duration_days,
                    amount,
                    currency,
                    invoice_payload,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            activated = cursor.rowcount == 1
            if activated:
                user_cursor = await self.connection.execute(
                    "SELECT subscription_ends_at FROM users WHERE chat_id = ?",
                    (chat_id,),
                )
                user_row = await user_cursor.fetchone()
                current_end = _dt(user_row["subscription_ends_at"])
                if lifetime:
                    # Бессрочный доступ хранится отдельно. Активный недельный
                    # тариф не обрывается и после окончания вернётся лимит 1.
                    await self.connection.execute(
                        """
                        UPDATE users
                        SET lifetime_access = 1, updated_at = ?
                        WHERE chat_id = ?
                        """,
                        (now.isoformat(), chat_id),
                    )
                    if not current_end or current_end <= now:
                        await self.connection.execute(
                            """
                            UPDATE subscriptions
                            SET interval_minutes = 15
                            WHERE chat_id = ? AND interval_minutes < 15
                            """,
                            (chat_id,),
                        )
                else:
                    # Если тариф ещё действует, новая неделя прибавляется к его
                    # окончанию. Иначе отсчёт начинается с текущего момента.
                    start_at = current_end if current_end and current_end > now else now
                    new_end = start_at + timedelta(days=duration_days)
                    await self.connection.execute(
                        """
                        UPDATE users
                        SET plan_code = ?, search_limit = ?,
                            subscription_ends_at = ?, updated_at = ?
                        WHERE chat_id = ?
                        """,
                        (
                            plan_code,
                            search_limit,
                            new_end.isoformat(),
                            now.isoformat(),
                            chat_id,
                        ),
                    )
            await self.connection.commit()
        return await self.get_access(chat_id, create=False), activated

    async def create_subscription(
        self,
        chat_id: int,
        url: str,
        interval_minutes: int,
        max_per_user: int,
    ) -> tuple[Subscription, bool]:
        """Создать поиск или обновить интервал уже существующего URL."""

        now = utc_now().isoformat()
        async with self._lock:
            cursor = await self.connection.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE chat_id = ?",
                (chat_id,),
            )
            count = (await cursor.fetchone())[0]
            existing = await self.connection.execute(
                "SELECT * FROM subscriptions WHERE chat_id = ? AND url = ?",
                (chat_id, url),
            )
            row = await existing.fetchone()
            if row:
                await self.connection.execute(
                    """
                    UPDATE subscriptions
                    SET interval_minutes = ?, active = 1, next_check_at = ?
                    WHERE id = ?
                    """,
                    (interval_minutes, now, row["id"]),
                )
                await self.connection.commit()
                updated = await self.get_subscription(row["id"], chat_id)
                assert updated is not None
                return updated, False
            if count >= max_per_user:
                raise ValueError(
                    f"Достигнут лимит: не более {max_per_user} подписок на пользователя"
                )
            cursor = await self.connection.execute(
                """
                INSERT INTO subscriptions
                    (chat_id, url, interval_minutes, next_check_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, url, interval_minutes, now, now),
            )
            await self.connection.commit()
            subscription = await self.get_subscription(cursor.lastrowid, chat_id)
            assert subscription is not None
            return subscription, True

    async def get_subscription(
        self, subscription_id: int, chat_id: int | None = None
    ) -> Subscription | None:
        """Найти подписку; chat_id дополнительно защищает чужие подписки."""

        sql = "SELECT * FROM subscriptions WHERE id = ?"
        params: list[int] = [subscription_id]
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        cursor = await self.connection.execute(sql, params)
        row = await cursor.fetchone()
        return _subscription(row) if row else None

    async def list_subscriptions(self, chat_id: int) -> list[Subscription]:
        """Вернуть все поиски одного Telegram-пользователя."""

        cursor = await self.connection.execute(
            "SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        )
        return [_subscription(row) for row in await cursor.fetchall()]

    async def due_subscriptions(self, limit: int = 100) -> list[Subscription]:
        """Выбрать активные поиски, которые пора проверить и разрешает тариф."""

        cursor = await self.connection.execute(
            """
            SELECT * FROM subscriptions
            WHERE active = 1 AND next_check_at <= ?
            ORDER BY next_check_at
            LIMIT ?
            """,
            (utc_now().isoformat(), limit * 10),
        )
        due = [_subscription(row) for row in await cursor.fetchall()]
        # Тариф ограничивает не общее число строк в БД, а число первых активных
        # поисков, которые реально попадают в мониторинг.
        access_cache: dict[int, AccessStatus] = {}
        allowed_ids_cache: dict[int, set[int]] = {}
        result: list[Subscription] = []
        for subscription in due:
            access = access_cache.get(subscription.chat_id)
            if access is None:
                access = await self.get_access(subscription.chat_id)
                access_cache[subscription.chat_id] = access
                allowed_cursor = await self.connection.execute(
                    """
                    SELECT id FROM subscriptions
                    WHERE chat_id = ? AND active = 1
                    ORDER BY id
                    LIMIT ?
                    """,
                    (subscription.chat_id, access.search_limit),
                )
                allowed_ids_cache[subscription.chat_id] = {
                    row["id"] for row in await allowed_cursor.fetchall()
                }
            if (
                access.active
                and subscription.id in allowed_ids_cache[subscription.chat_id]
            ):
                # Если недельный тариф закончился и остался бессрочный, старый
                # интервал 2/5/10 минут автоматически повышается до 15 минут.
                if subscription.interval_minutes < access.min_interval_minutes:
                    subscription.interval_minutes = access.min_interval_minutes
                    await self.connection.execute(
                        """
                        UPDATE subscriptions SET interval_minutes = ?
                        WHERE id = ?
                        """,
                        (access.min_interval_minutes, subscription.id),
                    )
                    await self.connection.commit()
                result.append(subscription)
                if len(result) >= limit:
                    break
        return result

    async def set_active(
        self, subscription_id: int, chat_id: int, active: bool
    ) -> bool:
        """Приостановить или возобновить поиск пользователя."""

        next_check = utc_now().isoformat()
        cursor = await self.connection.execute(
            """
            UPDATE subscriptions
            SET active = ?, next_check_at = ?
            WHERE id = ? AND chat_id = ?
            """,
            (int(active), next_check, subscription_id, chat_id),
        )
        await self.connection.commit()
        return cursor.rowcount == 1

    async def set_interval(
        self, subscription_id: int, chat_id: int, interval_minutes: int
    ) -> bool:
        """Изменить период и назначить ближайшую проверку на сейчас."""

        cursor = await self.connection.execute(
            """
            UPDATE subscriptions
            SET interval_minutes = ?, next_check_at = ?
            WHERE id = ? AND chat_id = ?
            """,
            (interval_minutes, utc_now().isoformat(), subscription_id, chat_id),
        )
        await self.connection.commit()
        return cursor.rowcount == 1

    async def force_due(self, subscription_id: int, chat_id: int) -> bool:
        """Поставить поиск в очередь немедленной проверки."""

        cursor = await self.connection.execute(
            """
            UPDATE subscriptions SET active = 1, next_check_at = ?
            WHERE id = ? AND chat_id = ?
            """,
            (utc_now().isoformat(), subscription_id, chat_id),
        )
        await self.connection.commit()
        return cursor.rowcount == 1

    async def delete_subscription(self, subscription_id: int, chat_id: int) -> bool:
        """Удалить поиск; связанные seen_ads и outbox удалятся по CASCADE."""

        cursor = await self.connection.execute(
            "DELETE FROM subscriptions WHERE id = ? AND chat_id = ?",
            (subscription_id, chat_id),
        )
        await self.connection.commit()
        return cursor.rowcount == 1

    async def initialize_baseline(
        self, subscription_id: int, listings: Iterable[Listing]
    ) -> int:
        """Запомнить первую выдачу без создания пользовательских уведомлений."""

        now = utc_now()
        items = list(listings)
        async with self._lock:
            await self.connection.executemany(
                """
                INSERT OR IGNORE INTO seen_ads
                    (subscription_id, item_id, first_seen_at, published_at, baseline)
                VALUES (?, ?, ?, ?, 1)
                """,
                [
                    (
                        subscription_id,
                        item.item_id,
                        now.isoformat(),
                        item.published_at.isoformat() if item.published_at else None,
                    )
                    for item in items
                ],
            )
            await self.connection.execute(
                """
                UPDATE subscriptions
                SET initialized_at = COALESCE(initialized_at, ?)
                WHERE id = ?
                """,
                (now.isoformat(), subscription_id),
            )
            await self.connection.commit()
        return len(items)

    async def unseen_item_ids(
        self, subscription_id: int, listings: Iterable[Listing]
    ) -> set[str]:
        """Вернуть ID карточек, которых ещё нет в истории этого поиска."""

        items = list(listings)
        if not items:
            return set()
        placeholders = ",".join("?" for _ in items)
        cursor = await self.connection.execute(
            f"""
            SELECT item_id FROM seen_ads
            WHERE subscription_id = ? AND item_id IN ({placeholders})
            """,
            [subscription_id, *[item.item_id for item in items]],
        )
        seen = {row["item_id"] for row in await cursor.fetchall()}
        return {item.item_id for item in items} - seen

    async def record_listing_and_enqueue(
        self, subscription: Subscription, listing: Listing, *, baseline: bool = False
    ) -> bool:
        """Атомарно запомнить объявление и поставить уведомление в outbox.

        Сначала вставляется уникальная пара (subscription_id, item_id). Только
        если она действительно новая, создаётся сообщение — это защита от дублей
        при перезапуске или одновременных проверках.
        """

        now = utc_now().isoformat()
        async with self._lock:
            cursor = await self.connection.execute(
                """
                INSERT OR IGNORE INTO seen_ads
                    (subscription_id, item_id, first_seen_at, published_at, baseline)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    subscription.id,
                    listing.item_id,
                    now,
                    listing.published_at.isoformat() if listing.published_at else None,
                    int(baseline),
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted and not baseline:
                await self.connection.execute(
                    """
                    INSERT OR IGNORE INTO outbox
                        (subscription_id, chat_id, item_id, title, price,
                         description, url, photo_url, created_at, next_attempt_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        subscription.id,
                        subscription.chat_id,
                        listing.item_id,
                        listing.title,
                        listing.price,
                        listing.description,
                        listing.url,
                        listing.photo_url,
                        now,
                        now,
                    ),
                )
            await self.connection.commit()
            return inserted

    async def mark_check_success(
        self, subscription_id: int, next_check_at: datetime
    ) -> None:
        """Записать успешную проверку и сбросить счётчик ошибок."""

        await self.connection.execute(
            """
            UPDATE subscriptions
            SET last_checked_at = ?, next_check_at = ?, last_error = NULL,
                error_count = 0
            WHERE id = ?
            """,
            (utc_now().isoformat(), next_check_at.isoformat(), subscription_id),
        )
        await self.connection.commit()

    async def mark_check_failure(
        self, subscription_id: int, error: str, next_check_at: datetime
    ) -> None:
        """Сохранить ошибку и время отложенного повтора."""

        await self.connection.execute(
            """
            UPDATE subscriptions
            SET last_checked_at = ?, next_check_at = ?, last_error = ?,
                error_count = error_count + 1
            WHERE id = ?
            """,
            (
                utc_now().isoformat(),
                next_check_at.isoformat(),
                error[:1000],
                subscription_id,
            ),
        )
        await self.connection.commit()

    async def claim_blocked_notice(self, subscription_id: int) -> bool:
        """Разрешить предупреждение о капче не чаще одного раза в шесть часов."""

        subscription = await self.get_subscription(subscription_id)
        if subscription is None:
            return False
        now = utc_now()
        if (
            subscription.blocked_notice_at
            and now - subscription.blocked_notice_at < timedelta(hours=6)
        ):
            return False
        await self.connection.execute(
            "UPDATE subscriptions SET blocked_notice_at = ? WHERE id = ?",
            (now.isoformat(), subscription_id),
        )
        await self.connection.commit()
        return True

    async def pending_outbox(self, limit: int = 25) -> list[OutboxMessage]:
        """Получить неотправленные уведомления, для которых наступил retry."""

        cursor = await self.connection.execute(
            """
            SELECT * FROM outbox
            WHERE sent_at IS NULL AND next_attempt_at <= ?
            ORDER BY id
            LIMIT ?
            """,
            (utc_now().isoformat(), limit),
        )
        return [
            OutboxMessage(
                id=row["id"],
                subscription_id=row["subscription_id"],
                chat_id=row["chat_id"],
                item_id=row["item_id"],
                title=row["title"],
                price=row["price"],
                description=row["description"],
                url=row["url"],
                photo_url=row["photo_url"],
                attempts=row["attempts"],
            )
            for row in await cursor.fetchall()
        ]

    async def mark_outbox_sent(self, outbox_id: int) -> None:
        """Пометить уведомление успешно доставленным."""

        await self.connection.execute(
            "UPDATE outbox SET sent_at = ?, last_error = NULL WHERE id = ?",
            (utc_now().isoformat(), outbox_id),
        )
        await self.connection.commit()

    async def mark_outbox_failed(
        self, outbox_id: int, attempts: int, error: str
    ) -> None:
        """Отложить неудачное уведомление с экспоненциальной задержкой."""

        delay_seconds = min(3600, 15 * (2 ** min(attempts, 8)))
        retry_at = utc_now() + timedelta(seconds=delay_seconds)
        await self.connection.execute(
            """
            UPDATE outbox
            SET attempts = ?, next_attempt_at = ?, last_error = ?
            WHERE id = ?
            """,
            (attempts, retry_at.isoformat(), error[:1000], outbox_id),
        )
        await self.connection.commit()
