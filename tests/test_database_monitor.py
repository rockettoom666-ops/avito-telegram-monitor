from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from avito_bot.avito import AvitoBlockedError, AvitoRateLimitError
from avito_bot.db import Database
from avito_bot.models import Listing
from avito_bot.monitor import MonitorService
from avito_bot.utils import utc_now


class FakeScraper:
    def __init__(self) -> None:
        self.details: list[str] = []

    async def fetch_detail(self, listing: Listing) -> Listing:
        self.details.append(listing.item_id)
        return listing


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text))


def listing(item_id: str, *, age_minutes: int = 0, photo_url: str = "") -> Listing:
    return Listing(
        item_id=item_id,
        title=f"MacBook {item_id}",
        price="75 000 ₽",
        description="16 ГБ, SSD 512 ГБ",
        url=f"https://www.avito.ru/item/macbook_{item_id}",
        published_at=utc_now() - timedelta(minutes=age_minutes),
        photo_url=photo_url,
    )


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "test.sqlite3")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_baseline_then_one_new_item_creates_one_outbox(db: Database) -> None:
    subscription, _ = await db.create_subscription(
        100, "https://www.avito.ru/search?s=104", 5, 10
    )
    monitor = MonitorService(db, FakeScraper(), FakeBot())
    await monitor.process_snapshot(subscription, [listing("111111111")])
    assert await db.pending_outbox() == []

    subscription = await db.get_subscription(subscription.id, 100)
    assert subscription is not None
    await monitor.process_snapshot(
        subscription,
        [
            listing("222222222", photo_url="https://cdn.example/macbook.jpg"),
            listing("111111111"),
        ],
    )
    pending = await db.pending_outbox()
    assert len(pending) == 1
    assert pending[0].item_id == "222222222"
    assert pending[0].photo_url == "https://cdn.example/macbook.jpg"

    subscription = await db.get_subscription(subscription.id, 100)
    assert subscription is not None
    await monitor.process_snapshot(
        subscription, [listing("222222222"), listing("111111111")]
    )
    assert len(await db.pending_outbox()) == 1


@pytest.mark.asyncio
async def test_complete_search_item_does_not_open_detail_page(db: Database) -> None:
    """Полная JSON-карточка сразу попадает в outbox без лишней загрузки."""

    subscription, _ = await db.create_subscription(
        106, "https://www.avito.ru/search?s=104", 5, 10
    )
    scraper = FakeScraper()
    monitor = MonitorService(db, scraper, FakeBot())
    await monitor.process_snapshot(subscription, [listing("111111111")])
    subscription = await db.get_subscription(subscription.id, 106)
    assert subscription is not None
    complete = listing(
        "222222222", photo_url="https://cdn.example/macbook.jpg"
    )
    complete.detail_complete = True

    await monitor.process_snapshot(subscription, [complete])

    assert scraper.details == []
    assert [item.item_id for item in await db.pending_outbox()] == ["222222222"]


@pytest.mark.asyncio
async def test_rate_limit_retry_uses_search_interval(db: Database) -> None:
    """Для 429 применяется интервал поиска без отдельного таймера."""

    subscription, _ = await db.create_subscription(
        107, "https://www.avito.ru/search?s=104", 2, 10
    )
    subscription.error_count = 20
    scraper = FakeScraper()
    scraper.manual_window_opened = False
    monitor = MonitorService(db, scraper, FakeBot())

    await monitor._record_failure(
        subscription,
        AvitoRateLimitError("429 Too Many Requests"),
    )

    saved = await db.get_subscription(subscription.id, subscription.chat_id)
    assert saved is not None
    assert saved.last_checked_at is not None
    delay = saved.next_check_at - saved.last_checked_at
    assert timedelta(minutes=1, seconds=59) <= delay <= timedelta(minutes=2)


@pytest.mark.asyncio
async def test_captcha_retry_uses_search_interval(db: Database) -> None:
    """Капча не должна включать прежнюю часовую блокировку поиска."""

    subscription, _ = await db.create_subscription(
        108, "https://www.avito.ru/search?s=104", 5, 10
    )
    subscription.error_count = 20
    scraper = FakeScraper()
    scraper.manual_window_opened = False
    monitor = MonitorService(db, scraper, FakeBot())

    await monitor._record_failure(
        subscription,
        AvitoBlockedError("Avito запросил проверку браузера"),
    )

    saved = await db.get_subscription(subscription.id, subscription.chat_id)
    assert saved is not None
    assert saved.last_checked_at is not None
    delay = saved.next_check_at - saved.last_checked_at
    assert timedelta(minutes=4, seconds=59) <= delay <= timedelta(minutes=5)


@pytest.mark.asyncio
async def test_old_resurfaced_listing_is_seen_but_not_notified(db: Database) -> None:
    subscription, _ = await db.create_subscription(
        101, "https://www.avito.ru/search?s=104", 5, 10
    )
    monitor = MonitorService(db, FakeScraper(), FakeBot())
    await monitor.process_snapshot(subscription, [listing("111111111")])
    subscription = await db.get_subscription(subscription.id, 101)
    assert subscription is not None

    await monitor.process_snapshot(subscription, [listing("333333333", age_minutes=60)])
    assert await db.pending_outbox() == []
    assert (
        await db.unseen_item_ids(
            subscription.id, [listing("333333333", age_minutes=60)]
        )
        == set()
    )


@pytest.mark.asyncio
async def test_state_survives_database_restart(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    first_db = Database(path)
    await first_db.connect()
    subscription, _ = await first_db.create_subscription(
        102, "https://www.avito.ru/search?s=104", 5, 10
    )
    await first_db.initialize_baseline(subscription.id, [listing("111111111")])
    await first_db.close()

    second_db = Database(path)
    await second_db.connect()
    try:
        restored = await second_db.get_subscription(subscription.id, 102)
        assert restored is not None
        assert restored.initialized_at is not None
        assert (
            await second_db.unseen_item_ids(subscription.id, [listing("111111111")])
            == set()
        )
    finally:
        await second_db.close()


@pytest.mark.asyncio
async def test_users_have_separate_subscriptions(db: Database) -> None:
    url = "https://www.avito.ru/search?s=104"
    first, _ = await db.create_subscription(1, url, 5, 10)
    second, _ = await db.create_subscription(2, url, 15, 10)
    assert first.chat_id == 1
    assert second.chat_id == 2
    assert len(await db.list_subscriptions(1)) == 1
    assert len(await db.list_subscriptions(2)) == 1


@pytest.mark.asyncio
async def test_user_gets_one_day_trial_with_one_search(db: Database) -> None:
    before = utc_now()
    access = await db.ensure_user(200)
    assert access.active
    assert access.is_trial
    assert access.search_limit == 1
    assert access.access_until >= before + timedelta(hours=23, minutes=59)


@pytest.mark.asyncio
async def test_paid_plan_activation_is_idempotent(db: Database) -> None:
    first_access, first_activation = await db.activate_plan(
        chat_id=201,
        plan_code="basic",
        search_limit=2,
        duration_days=7,
        provider="telegram_stars",
        provider_payment_id="charge-1",
        amount=150,
        currency="XTR",
        invoice_payload="subscription:v1:basic:201",
    )
    second_access, second_activation = await db.activate_plan(
        chat_id=201,
        plan_code="basic",
        search_limit=2,
        duration_days=7,
        provider="telegram_stars",
        provider_payment_id="charge-1",
        amount=150,
        currency="XTR",
        invoice_payload="subscription:v1:basic:201",
    )
    assert first_activation is True
    assert second_activation is False
    assert first_access.search_limit == 2
    assert first_access.is_trial is False
    assert second_access.access_until == first_access.access_until


@pytest.mark.asyncio
async def test_expired_trial_stops_due_searches(db: Database) -> None:
    await db.ensure_user(202)
    subscription, _ = await db.create_subscription(
        202, "https://www.avito.ru/search?q=one&s=104", 5, 10
    )
    await db.connection.execute(
        "UPDATE users SET trial_ends_at = ? WHERE chat_id = ?",
        ((utc_now() - timedelta(minutes=1)).isoformat(), 202),
    )
    await db.connection.commit()
    assert subscription.id not in {item.id for item in await db.due_subscriptions()}


@pytest.mark.asyncio
async def test_plan_monitors_only_its_search_limit(db: Database) -> None:
    await db.activate_plan(
        chat_id=203,
        plan_code="basic",
        search_limit=2,
        duration_days=7,
        provider="telegram_stars",
        provider_payment_id="charge-2",
        amount=150,
        currency="XTR",
        invoice_payload="subscription:v1:basic:203",
    )
    subscriptions = []
    for number in range(3):
        subscription, _ = await db.create_subscription(
            203,
            f"https://www.avito.ru/search?q={number}&s=104",
            5,
            10,
        )
        subscriptions.append(subscription)
    due_ids = {item.id for item in await db.due_subscriptions()}
    assert subscriptions[0].id in due_ids
    assert subscriptions[1].id in due_ids
    assert subscriptions[2].id not in due_ids


@pytest.mark.asyncio
async def test_lifetime_plan_is_permanent_and_raises_short_interval(
    db: Database,
) -> None:
    """Бессрочный тариф не имеет даты окончания и разрешает интервалы от 15 минут."""

    await db.ensure_user(204)
    subscription, _ = await db.create_subscription(
        204,
        "https://www.avito.ru/search?q=lifetime&s=104",
        5,
        10,
    )

    access, activated = await db.activate_plan(
        chat_id=204,
        plan_code="lifetime",
        search_limit=1,
        duration_days=0,
        lifetime=True,
        provider="yookassa",
        provider_payment_id="lifetime-charge-1",
        amount=10_000,
        currency="RUB",
        invoice_payload="subscription:yookassa:v1:lifetime:204",
    )

    assert activated is True
    assert access.active is True
    assert access.is_lifetime is True
    assert access.search_limit == 1
    assert access.access_until is None
    assert access.min_interval_minutes == 15
    saved = await db.get_subscription(subscription.id, 204)
    assert saved is not None
    assert saved.interval_minutes == 15


@pytest.mark.asyncio
async def test_weekly_plan_temporarily_overrides_lifetime_plan(db: Database) -> None:
    """После недельного тарифа бессрочный доступ автоматически возвращается."""

    await db.activate_plan(
        chat_id=205,
        plan_code="lifetime",
        search_limit=1,
        duration_days=0,
        lifetime=True,
        provider="telegram_stars",
        provider_payment_id="lifetime-charge-2",
        amount=75,
        currency="XTR",
        invoice_payload="subscription:v1:lifetime:205",
    )
    weekly_access, _ = await db.activate_plan(
        chat_id=205,
        plan_code="pro",
        search_limit=5,
        duration_days=7,
        provider="telegram_stars",
        provider_payment_id="weekly-charge-after-lifetime",
        amount=250,
        currency="XTR",
        invoice_payload="subscription:v1:pro:205",
    )
    assert weekly_access.is_lifetime is False
    assert weekly_access.search_limit == 5
    assert weekly_access.min_interval_minutes == 2

    await db.connection.execute(
        "UPDATE users SET subscription_ends_at = ? WHERE chat_id = ?",
        ((utc_now() - timedelta(minutes=1)).isoformat(), 205),
    )
    await db.connection.commit()

    fallback_access = await db.get_access(205)
    assert fallback_access.active is True
    assert fallback_access.is_lifetime is True
    assert fallback_access.search_limit == 1
    assert fallback_access.access_until is None


@pytest.mark.asyncio
async def test_admin_gets_permanent_maximum_access(db: Database) -> None:
    """Админский доступ бессрочен и не уменьшается после тестовой оплаты."""

    await db.sync_admin_access(frozenset({300}))
    access = await db.get_access(300)
    assert access.active is True
    assert access.is_lifetime is True
    assert access.plan_code == "admin"
    assert access.search_limit == 10
    assert access.access_until is None
    assert access.min_interval_minutes == 2

    paid_access, _ = await db.activate_plan(
        chat_id=300,
        plan_code="basic",
        search_limit=2,
        duration_days=7,
        provider="yookassa_test",
        provider_payment_id="admin-test-payment",
        amount=8_800,
        currency="RUB",
        invoice_payload="subscription:yookassa_test:v1:basic:300",
    )
    assert paid_access.plan_code == "admin"
    assert paid_access.search_limit == 10
    assert paid_access.access_until is None


@pytest.mark.asyncio
async def test_removing_admin_id_restores_ordinary_access(db: Database) -> None:
    """Удалённый из ADMIN_CHAT_IDS пользователь теряет только админскую льготу."""

    await db.sync_admin_access(frozenset({301}))
    await db.connection.execute(
        "UPDATE users SET trial_ends_at = ? WHERE chat_id = ?",
        ((utc_now() - timedelta(minutes=1)).isoformat(), 301),
    )
    await db.connection.commit()

    await db.sync_admin_access(frozenset())
    access = await db.get_access(301)
    assert access.active is False
    assert access.plan_code is None
