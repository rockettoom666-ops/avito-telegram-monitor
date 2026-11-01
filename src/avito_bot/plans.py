"""Описание продаваемых тарифов подписки."""

from __future__ import annotations

from dataclasses import dataclass

from avito_bot.config import Settings


@dataclass(frozen=True, slots=True)
class SubscriptionPlan:
    """Неизменяемые параметры одного тарифа."""

    code: str
    title: str
    search_limit: int
    duration_days: int
    rub_price: int
    stars_price: int
    lifetime: bool = False
    min_interval_minutes: int = 2


def format_search_count(count: int) -> str:
    """Вернуть число с правильной формой слова «поиск»."""

    last_two_digits = count % 100
    last_digit = count % 10
    if last_digit == 1 and last_two_digits != 11:
        word = "поиск"
    elif last_digit in {2, 3, 4} and last_two_digits not in {12, 13, 14}:
        word = "поиска"
    else:
        word = "поисков"
    return f"{count} {word}"


def get_plans(settings: Settings) -> dict[str, SubscriptionPlan]:
    """Вернуть тарифы по их внутренним кодам.

    Цена в рублях показывается пользователю как справочная, а внутри Telegram
    цифровая услуга оплачивается Stars. Количество Stars задаётся через .env.
    """

    return {
        "basic": SubscriptionPlan(
            code="basic",
            title="Старт",
            search_limit=2,
            duration_days=7,
            rub_price=200,
            stars_price=settings.basic_plan_stars,
        ),
        "pro": SubscriptionPlan(
            code="pro",
            title="Про",
            search_limit=5,
            duration_days=7,
            rub_price=350,
            stars_price=settings.pro_plan_stars,
        ),
        "lifetime": SubscriptionPlan(
            code="lifetime",
            title="Навсегда",
            search_limit=1,
            # Ноль хранится в истории платежа как признак отсутствия срока.
            duration_days=0,
            rub_price=settings.lifetime_plan_rubles,
            stars_price=settings.lifetime_plan_stars,
            lifetime=True,
            min_interval_minutes=15,
        ),
    }


def plan_period_text(plan: SubscriptionPlan) -> str:
    """Вернуть понятный срок действия тарифа для кнопок и счетов."""

    return "навсегда" if plan.lifetime else f"на {plan.duration_days} дней"
