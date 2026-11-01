"""Проверки обоих встроенных способов оплаты Telegram."""

import json
from unittest.mock import AsyncMock

import pytest

from avito_bot.handlers import (
    _payload_details,
    _payload_plan,
    _payment_matches,
    send_subscription_invoice,
    send_yookassa_invoice,
)
from avito_bot.keyboards import (
    interval_keyboard,
    payment_confirmation_keyboard,
    subscription_interval_keyboard,
)
from avito_bot.plans import SubscriptionPlan, plan_period_text


def sample_plan() -> SubscriptionPlan:
    """Тариф, общий для проверок Stars и ЮKassa."""

    return SubscriptionPlan(
        code="basic",
        title="Старт",
        search_limit=2,
        duration_days=7,
        rub_price=200,
        stars_price=150,
    )


def lifetime_plan() -> SubscriptionPlan:
    """Бессрочный тариф за 100 ₽ с одним поиском."""

    return SubscriptionPlan(
        code="lifetime",
        title="Навсегда",
        search_limit=1,
        duration_days=0,
        rub_price=100,
        stars_price=75,
        lifetime=True,
        min_interval_minutes=15,
    )


@pytest.mark.asyncio
async def test_send_invoice_uses_required_telegram_stars_fields() -> None:
    """Счёт Stars должен содержать XTR, цену и пустой provider_token."""

    bot = AsyncMock()
    bot.send_invoice.return_value = object()

    await send_subscription_invoice(bot, 123456789, sample_plan())

    arguments = bot.send_invoice.await_args.kwargs
    assert arguments["chat_id"] == 123456789
    assert arguments["provider_token"] == ""
    assert arguments["currency"] == "XTR"
    assert arguments["prices"][0].amount == 150
    assert arguments["start_parameter"] == "subscribe_basic"
    assert _payload_details(arguments["payload"], 123456789) == (
        "telegram_stars",
        "basic",
    )


@pytest.mark.asyncio
async def test_yookassa_send_invoice_contains_rubles_and_receipt() -> None:
    """ЮKassa получает BotFather-токен, сумму в копейках и данные чека."""

    bot = AsyncMock()
    bot.send_invoice.return_value = object()

    await send_yookassa_invoice(
        bot,
        123456789,
        sample_plan(),
        "123456:test-provider-token",
        vat_code=1,
    )

    arguments = bot.send_invoice.await_args.kwargs
    receipt = json.loads(arguments["provider_data"])["receipt"]
    item = receipt["items"][0]

    assert arguments["chat_id"] == 123456789
    assert arguments["provider_token"] == "123456:test-provider-token"
    assert arguments["currency"] == "RUB"
    assert arguments["prices"][0].amount == 20_000
    assert arguments["need_email"] is True
    assert arguments["send_email_to_provider"] is True
    assert item["amount"] == {"value": "200.00", "currency": "RUB"}
    assert item["vat_code"] == 1
    assert item["payment_mode"] == "full_payment"
    assert item["payment_subject"] == "service"
    assert _payload_details(arguments["payload"], 123456789) == (
        "yookassa",
        "basic",
    )


@pytest.mark.asyncio
async def test_lifetime_yookassa_invoice_costs_100_rubles() -> None:
    """Бессрочный счёт содержит цену 100 ₽ и не показывает срок «0 дней»."""

    bot = AsyncMock()
    bot.send_invoice.return_value = object()

    await send_yookassa_invoice(
        bot,
        123456789,
        lifetime_plan(),
        "123456:test-provider-token",
        send_receipt=False,
    )

    arguments = bot.send_invoice.await_args.kwargs
    assert arguments["prices"][0].amount == 10_000
    assert "навсегда" in arguments["description"]
    assert "0 дней" not in arguments["description"]
    assert _payload_details(arguments["payload"], 123456789) == (
        "yookassa",
        "lifetime",
    )


@pytest.mark.asyncio
async def test_yookassa_receipt_can_be_disabled() -> None:
    """Для магазина со своей кассой поля «Чеков от ЮKassa» отключаются."""

    bot = AsyncMock()
    bot.send_invoice.return_value = object()

    await send_yookassa_invoice(
        bot,
        123,
        sample_plan(),
        "123456:test-provider-token",
        send_receipt=False,
    )

    arguments = bot.send_invoice.await_args.kwargs
    assert "provider_data" not in arguments
    assert "need_email" not in arguments


@pytest.mark.asyncio
async def test_admin_yookassa_invoice_uses_telegram_minimum() -> None:
    """Админский счёт использует минимальную допустимую целую сумму 88 ₽."""

    bot = AsyncMock()
    bot.send_invoice.return_value = object()

    await send_yookassa_invoice(
        bot,
        123,
        sample_plan(),
        "123456:test-provider-token",
        test_payment=True,
    )

    arguments = bot.send_invoice.await_args.kwargs
    item = json.loads(arguments["provider_data"])["receipt"]["items"][0]
    assert arguments["prices"][0].amount == 8_800
    assert item["amount"]["value"] == "88.00"
    assert _payload_details(arguments["payload"], 123) == (
        "yookassa_test",
        "basic",
    )


def test_reduced_test_payment_requires_admin_access() -> None:
    """Подделанный тестовый счёт обычного пользователя не принимается."""

    plan = sample_plan()
    assert _payment_matches(
        "yookassa_test",
        plan,
        "RUB",
        8_800,
        allow_admin_test=True,
    )
    assert not _payment_matches(
        "yookassa_test",
        plan,
        "RUB",
        8_800,
        allow_admin_test=False,
    )
    assert not _payment_matches(
        "yookassa_test",
        plan,
        "RUB",
        20_000,
        allow_admin_test=True,
    )


def test_invoice_payload_cannot_be_used_by_another_chat() -> None:
    """Служебный payload счёта нельзя принять от другого пользователя."""

    assert _payload_plan("subscription:v1:pro:111", 222) is None
    assert (
        _payload_details("subscription:yookassa:v1:pro:111", 222)
        is None
    )
    assert (
        _payload_details("subscription:yookassa_test:v1:pro:111", 222)
        is None
    )


def test_payment_keyboard_contains_stars_and_yookassa() -> None:
    """После тарифа показываются два ожидаемых способа оплаты."""

    keyboard = payment_confirmation_keyboard(sample_plan())
    buttons = [row[0] for row in keyboard.inline_keyboard[:2]]
    assert buttons[0].text == "⭐ TG Stars — 150 ⭐"
    assert buttons[0].callback_data == "pay:stars:basic"
    assert buttons[1].text == "💳 ЮKassa (карты, СБП) — 200 ₽"
    assert buttons[1].callback_data == "pay:yookassa:basic"


def test_admin_keyboard_contains_reduced_price_test_button() -> None:
    """Тестовая кнопка добавляется только при явном флаге администратора."""

    public_keyboard = payment_confirmation_keyboard(sample_plan())
    admin_keyboard = payment_confirmation_keyboard(
        sample_plan(),
        show_test_payment=True,
    )

    assert all(
        row[0].callback_data != "pay:yookassa_test:basic"
        for row in public_keyboard.inline_keyboard
    )
    assert admin_keyboard.inline_keyboard[2][0].text == "🧪 Тест ЮKassa — 88 ₽"
    assert (
        admin_keyboard.inline_keyboard[2][0].callback_data
        == "pay:yookassa_test:basic"
    )


def test_lifetime_plan_only_offers_intervals_from_15_minutes() -> None:
    """Клавиатуры бессрочного тарифа скрывают интервалы 2, 5 и 10 минут."""

    new_search = interval_keyboard(minimum_minutes=15)
    existing_search = subscription_interval_keyboard(42, minimum_minutes=15)

    assert [
        button.text
        for row in new_search.inline_keyboard
        for button in row
    ] == ["15 мин", "30 мин", "60 мин"]
    assert [
        button.callback_data
        for row in existing_search.inline_keyboard
        for button in row
    ] == ["subint:42:15", "subint:42:30", "subint:42:60"]
    assert plan_period_text(lifetime_plan()) == "навсегда"
