"""Небольшие функции для URL, дат и безопасного текста уведомлений."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
ALLOWED_INTERVALS = (2, 5, 10, 15, 30, 60)
ITEM_ID_RE = re.compile(r"_(\d{6,})(?:[/?#]|$)")


def utc_now() -> datetime:
    """Текущее время с часовым поясом UTC для единообразной записи в БД."""

    return datetime.now(tz=ZoneInfo("UTC"))


def normalize_avito_url(raw_url: str) -> str:
    """Проверить ссылку Avito и привести её к единому виду.

    Номер страницы удаляется, а сортировка всегда заменяется на «сначала новые».
    Благодаря этому одинаковые поиски не сохраняются как разные подписки.
    """

    value = raw_url.strip()
    parts = urlsplit(value)
    hostname = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Ссылка должна начинаться с http:// или https://")
    if hostname != "avito.ru" and not hostname.endswith(".avito.ru"):
        raise ValueError("Поддерживаются только ссылки поиска на avito.ru")
    if not parts.path or parts.path == "/":
        raise ValueError("Нужна ссылка на страницу поиска, а не главная Avito")

    original_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = [
        (key, value)
        for key, value in original_pairs
        if key.lower() not in {"s", "p", "page"}
    ]
    if not any(key.lower() == "cd" for key, _ in query):
        query.append(("cd", "1"))
    query.append(("s", "104"))
    normalized_query = urlencode(sorted(query), doseq=True)
    return urlunsplit(
        ("https", "www.avito.ru", parts.path.rstrip("/"), normalized_query, "")
    )


def extract_item_id(url: str, fallback: str = "") -> str:
    """Получить стабильный ID объявления из атрибута карточки или её URL."""

    if fallback and fallback.isdigit():
        return fallback
    match = ITEM_ID_RE.search(urlsplit(url).path)
    if not match:
        raise ValueError(f"Не удалось определить ID объявления: {url}")
    return match.group(1)


def parse_published_text(value: str, now: datetime | None = None) -> datetime | None:
    """Преобразовать текст Avito вроде «5 минут назад» в дату UTC."""

    text = " ".join(value.lower().replace("\xa0", " ").split())
    if not text:
        return None
    current = now or utc_now()
    minute_match = re.search(r"(\d+)\s+мин", text)
    if minute_match:
        return current - timedelta(minutes=int(minute_match.group(1)))
    hour_match = re.search(r"(\d+)\s+час", text)
    if hour_match:
        return current - timedelta(hours=int(hour_match.group(1)))
    if "только что" in text:
        return current

    local_now = current.astimezone(MOSCOW_TZ)
    time_match = re.search(r"(\d{1,2}):(\d{2})", text)
    if "сегодня" in text and time_match:
        return local_now.replace(
            hour=int(time_match.group(1)),
            minute=int(time_match.group(2)),
            second=0,
            microsecond=0,
        ).astimezone(ZoneInfo("UTC"))
    if "вчера" in text and time_match:
        return (
            local_now.replace(
                hour=int(time_match.group(1)),
                minute=int(time_match.group(2)),
                second=0,
                microsecond=0,
            )
            - timedelta(days=1)
        ).astimezone(ZoneInfo("UTC"))
    return None


def format_listing_message(
    title: str,
    price: str,
    description: str,
    url: str,
) -> str:
    """Сформировать HTML-текст уведомления без фотографии.

    Пользовательский текст экранируется, чтобы разметка объявления не сломала
    HTML-разметку сообщения Telegram.
    """

    safe_title = html.escape(title.strip() or "Новое объявление")
    safe_price = html.escape(price.strip() or "Цена не указана")
    clean_description = " ".join(description.split()) or "Описание отсутствует"
    if len(clean_description) > 2600:
        clean_description = clean_description[:2597].rstrip() + "…"
    safe_description = html.escape(clean_description)
    safe_url = html.escape(url, quote=True)
    return (
        f"🆕 <b>{safe_title}</b>\n\n"
        f"💰 <b>{safe_price}</b>\n\n"
        f"📝 {safe_description}\n\n"
        f'🔗 <a href="{safe_url}">Открыть объявление</a>'
    )


def format_listing_caption(
    title: str,
    price: str,
    description: str,
    url: str,
) -> str:
    """Сформировать подпись к фото не длиннее лимита Telegram в 1024 символа."""

    clean_description = " ".join(description.split()) or "Описание отсутствует"
    description_limit = min(500, len(clean_description))
    while description_limit >= 0:
        shortened = clean_description[:description_limit].rstrip()
        if description_limit < len(clean_description):
            shortened += "…"
        caption = format_listing_message(title[:180], price[:100], shortened, url)
        if len(caption) <= 1024:
            return caption
        description_limit -= max(20, len(caption) - 1000)
    return format_listing_message(title[:120], price[:80], "Описание по ссылке", url)


def format_dt(value: datetime | None) -> str:
    """Показать хранимую UTC-дату пользователю по московскому времени."""

    if value is None:
        return "—"
    return value.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M:%S")
