"""Фабрики inline-клавиатур, которые бот прикрепляет к сообщениям."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from avito_bot.models import Subscription
from avito_bot.plans import SubscriptionPlan, format_search_count, plan_period_text
from avito_bot.utils import ALLOWED_INTERVALS


def interval_keyboard(
    prefix: str = "newint",
    *,
    minimum_minutes: int = 2,
) -> InlineKeyboardMarkup:
    """Кнопки выбора интервала для нового поиска."""

    rows: list[list[InlineKeyboardButton]] = []
    buttons = [
        InlineKeyboardButton(text=f"{minutes} мин", callback_data=f"{prefix}:{minutes}")
        for minutes in ALLOWED_INTERVALS
        if minutes >= minimum_minutes
    ]
    for index in range(0, len(buttons), 3):
        rows.append(buttons[index : index + 3])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_keyboard(subscription: Subscription) -> InlineKeyboardMarkup:
    """Кнопки управления уже созданным поиском."""

    action = "resume" if not subscription.active else "pause"
    action_text = "▶️ Возобновить" if not subscription.active else "⏸ Приостановить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Проверить сейчас",
                    callback_data=f"sub:check:{subscription.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏱ Интервал",
                    callback_data=f"sub:interval:{subscription.id}",
                ),
                InlineKeyboardButton(
                    text=action_text,
                    callback_data=f"sub:{action}:{subscription.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"sub:delete:{subscription.id}",
                )
            ],
        ]
    )


def subscription_interval_keyboard(
    subscription_id: int,
    *,
    minimum_minutes: int = 2,
) -> InlineKeyboardMarkup:
    """Кнопки изменения интервала конкретной подписки."""

    rows: list[list[InlineKeyboardButton]] = []
    buttons = [
        InlineKeyboardButton(
            text=f"{minutes} мин",
            callback_data=f"subint:{subscription_id}:{minutes}",
        )
        for minutes in ALLOWED_INTERVALS
        if minutes >= minimum_minutes
    ]
    for index in range(0, len(buttons), 3):
        rows.append(buttons[index : index + 3])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_keyboard(
    plans: dict[str, SubscriptionPlan],
) -> InlineKeyboardMarkup:
    """Витрина тарифов с ценой и лимитом поисков."""

    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{plan.title}: {format_search_count(plan.search_limit)}, "
                    f"{plan_period_text(plan)} — "
                    f"{plan.rub_price} ₽ / {plan.stars_price} ⭐"
                ),
                callback_data=f"buy:{plan.code}",
            )
        ]
        for plan in plans.values()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_confirmation_keyboard(
    plan: SubscriptionPlan,
    *,
    show_test_payment: bool = False,
    test_payment_rubles: int = 88,
) -> InlineKeyboardMarkup:
    """Выбор оплаты; тестовая цена показывается только владельцу бота."""

    rows = [
        [
            InlineKeyboardButton(
                text=f"⭐ TG Stars — {plan.stars_price} ⭐",
                callback_data=f"pay:stars:{plan.code}",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"💳 ЮKassa (карты, СБП) — {plan.rub_price} ₽",
                callback_data=f"pay:yookassa:{plan.code}",
            )
        ],
    ]
    if show_test_payment:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🧪 Тест ЮKassa — {test_payment_rubles} ₽",
                    callback_data=f"pay:yookassa_test:{plan.code}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="📄 Условия использования",
                callback_data="terms:show",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
