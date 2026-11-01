"""Простые структуры данных, которыми обмениваются части приложения."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Listing:
    """Одно объявление Avito после извлечения из выдачи или карточки товара."""

    item_id: str
    title: str
    price: str
    description: str
    url: str
    published_at: datetime | None = None
    published_text: str = ""
    photo_url: str = ""
    # True означает, что поиск уже вернул все данные для уведомления. Тогда
    # монитор не открывает карточку отдельно и экономит один запрос к Avito.
    detail_complete: bool = False


@dataclass(slots=True)
class Subscription:
    """Сохранённый поиск пользователя и состояние его последней проверки."""

    id: int
    chat_id: int
    url: str
    interval_minutes: int
    active: bool
    initialized_at: datetime | None
    next_check_at: datetime
    last_checked_at: datetime | None
    last_error: str | None
    error_count: int
    blocked_notice_at: datetime | None
    created_at: datetime


@dataclass(slots=True)
class OutboxMessage:
    """Уведомление, которое ждёт отправки пользователю через Telegram."""

    id: int
    subscription_id: int
    chat_id: int
    item_id: str
    title: str
    price: str
    description: str
    url: str
    photo_url: str
    attempts: int


@dataclass(frozen=True, slots=True)
class AccessStatus:
    """Текущий доступ пользователя: пробный период или платный тариф."""

    chat_id: int
    active: bool
    is_trial: bool
    is_lifetime: bool
    plan_code: str | None
    search_limit: int
    access_until: datetime | None
    min_interval_minutes: int
